"""
Cliente Socket.IO para conectar este dispositivo (zapy) ao servidor ZAccess.
Escuta relay:toggle; envia relay:state-update, input:state-update e heartbeat (telemetria).
A conexão em si é mantida pelo ping nativo do Socket.IO; heartbeat é opcional/complementar.
Ref: https://github.com/Sys-Bernardo-Rodrigues/Projeto-ZAccess
"""
import os
import logging
import threading
import time

import socketio

logger = logging.getLogger(__name__)

NAMESPACE = "/devices"
HEARTBEAT_INTERVAL = 30  # telemetria periódica (liveness = ping nativo Socket.IO)
INPUT_PUSH_INTERVAL = 30 # backup: reenvio periódico; mudanças reais são enviadas na hora via callback
RECONNECT_DELAY = 5
RECONNECT_MAX_DELAY = 120


def _state_from_value(value: bool) -> str:
    """Converte valor do relé (True/False) para estado ZAccess ('open'/'closed')."""
    return "open" if value else "closed"


def run_zaccess_client(
    server_url: str,
    serial_number: str,
    reles: dict,
    auth_token: str | None = None,
    sensores: dict | None = None,
    sensor_pins: dict | None = None,
):
    """
    Conecta ao ZAccess via Socket.IO (namespace /devices).
    Envia relay:state-update e input:state-update (sensores/botões) quando configurado.
    """
    auth = {"serialNumber": serial_number}
    if auth_token:
        auth["authToken"] = auth_token

    relay_id_by_channel: dict[int, str] = {}
    input_id_by_gpio: dict[int, str] = {}
    reconnect_delay = RECONNECT_DELAY

    while True:
        input_push_stop = threading.Event()
        heartbeat_stop = threading.Event()
        session_started = time.monotonic()
        sio = socketio.Client(logger=False, engineio_logger=False)

        def emit_heartbeat() -> bool:
            """Heartbeat de aplicação (telemetria). Não substitui o ping nativo do Socket.IO."""
            if not sio.connected:
                return False
            try:
                sio.emit(
                    "heartbeat",
                    {"uptimeSec": int(time.monotonic() - session_started)},
                    namespace=NAMESPACE,
                )
                return True
            except Exception as e:
                logger.warning("ZAccess: heartbeat falhou - %s", e)
                return False

        @sio.event(namespace=NAMESPACE)
        def connect():
            nonlocal reconnect_delay, session_started
            reconnect_delay = RECONNECT_DELAY
            session_started = time.monotonic()
            logger.info("ZAccess: conectado ao servidor %s", server_url)
            emit_heartbeat()

        @sio.event(namespace=NAMESPACE)
        def connect_error(data):
            logger.warning("ZAccess: erro de conexão - %s", data)

        @sio.event(namespace=NAMESPACE)
        def disconnect():
            logger.warning("ZAccess: desconectado; reconectando em %ss...", reconnect_delay)

        @sio.event(namespace=NAMESPACE)
        def error(data):
            logger.error("ZAccess: erro do servidor - %s", data)

        def push_input_states():
            """Envia estado de todos os sensores/botões para o ZAccess (input:state-update)."""
            if not sensores or not sensor_pins or not sio.connected:
                return
            for sid, pin in sensor_pins.items():
                if sid not in sensores:
                    continue
                input_id = input_id_by_gpio.get(pin)
                if not input_id:
                    continue
                try:
                    val = getattr(sensores[sid], "value", None)
                    if val is None:
                        continue
                    state = "inactive" if val else "active"
                    sio.emit(
                        "input:state-update",
                        {"inputId": input_id, "state": state},
                        namespace=NAMESPACE,
                    )
                except Exception as e:
                    logger.debug("ZAccess: input push %s - %s", sid, e)

        def _emit_input_state(input_id: str, state: str):
            """Envia um input:state-update imediato (callback de GPIO)."""
            try:
                if sio.connected:
                    sio.emit(
                        "input:state-update",
                        {"inputId": input_id, "state": state},
                        namespace=NAMESPACE,
                    )
            except Exception:
                pass

        @sio.on("device:config", namespace=NAMESPACE)
        def device_config(data):
            relay_id_by_channel.clear()
            for r in data.get("relays") or []:
                ch = r.get("channel")
                rid = r.get("id")
                if ch is not None and rid is not None:
                    relay_id_by_channel[int(ch)] = str(rid)
            input_id_by_gpio.clear()
            for i in data.get("inputs") or []:
                gpio = i.get("gpioPin")
                iid = i.get("id")
                if gpio is not None and iid is not None:
                    input_id_by_gpio[int(gpio)] = str(iid)
            logger.info("ZAccess: config recebida, relés: %s, inputs: %s", list(relay_id_by_channel.keys()), list(input_id_by_gpio.keys()))
            push_input_states()
            # Callbacks: envio instantâneo ao mudar GPIO (activated=inactive, deactivated=active para ZAccess)
            if sensores and sensor_pins:
                for sid, dev in sensores.items():
                    pin = sensor_pins.get(sid)
                    input_id = input_id_by_gpio.get(pin) if pin is not None else None
                    if not input_id or not hasattr(dev, "when_activated"):
                        continue
                    iid = str(input_id)
                    try:
                        dev.when_activated = lambda i=iid: _emit_input_state(i, "inactive")
                        dev.when_deactivated = lambda i=iid: _emit_input_state(i, "active")
                    except Exception as e:
                        logger.debug("ZAccess: callback input %s - %s", sid, e)

        pulse_timers: dict[str, threading.Timer] = {}
        pulse_timers_lock = threading.Lock()

        @sio.on("relay:toggle", namespace=NAMESPACE)
        def relay_toggle(data):
            channel = data.get("channel")
            target_state = data.get("targetState", "closed")
            relay_id = data.get("relayId")
            mode = data.get("mode") or "toggle"
            pulse_duration_ms = int(data.get("pulseDuration") or 1000)
            if channel is None:
                logger.warning("ZAccess: relay:toggle sem channel - %s", data)
                return
            key = str(channel)
            if key not in reles:
                logger.warning("ZAccess: relay:toggle canal inexistente %s", channel)
                return
            rid = str(relay_id) if relay_id else relay_id_by_channel.get(int(channel))

            def close_and_notify():
                with pulse_timers_lock:
                    pulse_timers.pop(key, None)
                if key in reles:
                    reles[key].value = False
                if rid:
                    try:
                        sio.emit(
                            "relay:state-update",
                            {"relayId": rid, "state": "closed"},
                            namespace=NAMESPACE,
                        )
                    except Exception:
                        pass
                logger.info("ZAccess: relé canal %s -> closed (fim do pulso)", channel)

            if mode == "pulse" and target_state == "open":
                with pulse_timers_lock:
                    old = pulse_timers.pop(key, None)
                    if old:
                        old.cancel()
                reles[key].value = True
                if rid:
                    try:
                        sio.emit(
                            "relay:state-update",
                            {"relayId": rid, "state": "open"},
                            namespace=NAMESPACE,
                        )
                    except Exception:
                        pass
                logger.info("ZAccess: relé canal %s -> open (pulso %s ms)", channel, pulse_duration_ms)
                t = threading.Timer(pulse_duration_ms / 1000.0, close_and_notify)
                t.daemon = True
                with pulse_timers_lock:
                    pulse_timers[key] = t
                t.start()
                return

            reles[key].value = target_state == "open"
            state = _state_from_value(reles[key].value)
            if rid:
                try:
                    sio.emit(
                        "relay:state-update",
                        {"relayId": rid, "state": state},
                        namespace=NAMESPACE,
                    )
                except Exception:
                    pass
            logger.info("ZAccess: relé canal %s -> %s", channel, state)

        def heartbeat_loop():
            while not heartbeat_stop.is_set():
                if heartbeat_stop.wait(timeout=HEARTBEAT_INTERVAL):
                    break
                if not sio.connected:
                    continue
                emit_heartbeat()

        def input_push_loop():
            """Envia estado dos inputs a cada INPUT_PUSH_INTERVAL."""
            while not input_push_stop.is_set():
                if input_push_stop.wait(timeout=INPUT_PUSH_INTERVAL):
                    break
                if not sio.connected:
                    continue
                push_input_states()

        try:
            sio.connect(
                server_url,
                auth=auth,
                namespaces=[NAMESPACE],
                transports=["websocket", "polling"],
            )
            t = threading.Thread(target=heartbeat_loop, daemon=True)
            t.start()
            if sensores and sensor_pins:
                t_in = threading.Thread(target=input_push_loop, daemon=True)
                t_in.start()
            sio.wait()
        except Exception as e:
            logger.warning("ZAccess: conexão encerrada - %s", e)
        finally:
            heartbeat_stop.set()
            input_push_stop.set()
            if sio.connected:
                try:
                    sio.disconnect()
                except Exception:
                    pass

        logger.info("ZAccess: reconectando em %ss...", reconnect_delay)
        time.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX_DELAY)


def start_zaccess_client_in_background(
    reles: dict,
    sensores: dict | None = None,
    sensor_pins: dict | None = None,
) -> threading.Thread | None:
    """
    Inicia o cliente ZAccess em uma thread daemon.
    Se sensores e sensor_pins forem passados, envia input:state-update para o ZAccess.
    """
    url = os.environ.get("ZACCESS_SERVER_URL", "").strip()
    serial = os.environ.get("ZACCESS_DEVICE_SERIAL", "").strip()
    if not url or not serial:
        logger.info(
            "ZAccess: não configurado (ZACCESS_SERVER_URL e ZACCESS_DEVICE_SERIAL necessários)"
        )
        return None
    token = os.environ.get("ZACCESS_DEVICE_TOKEN", "").strip() or None

    def run():
        run_zaccess_client(
            url, serial, reles,
            auth_token=token,
            sensores=sensores,
            sensor_pins=sensor_pins,
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    logger.info("ZAccess: cliente iniciado em background -> %s", url)
    return thread

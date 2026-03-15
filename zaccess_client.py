"""
Cliente Socket.IO para conectar este dispositivo (zapy) ao servidor ZAccess.
Escuta o evento relay:toggle e envia relay:state-update e heartbeat.
Reconecta automaticamente quando a conexão cai.
Ref: https://github.com/Sys-Bernardo-Rodrigues/Projeto-ZAccess
"""
import os
import logging
import threading
import time

import socketio

logger = logging.getLogger(__name__)

NAMESPACE = "/devices"
HEARTBEAT_INTERVAL = 20  # segundos (servidor espera ~90s; enviar bem antes)
RECONNECT_DELAY = 10  # segundos antes de tentar reconectar
RECONNECT_MAX_DELAY = 300  # cap do backoff (5 min)


def _state_from_value(value: bool) -> str:
    """Converte valor do relé (True/False) para estado ZAccess ('open'/'closed')."""
    return "open" if value else "closed"


def run_zaccess_client(
    server_url: str,
    serial_number: str,
    reles: dict,
    auth_token: str | None = None,
):
    """
    Conecta ao ZAccess via Socket.IO (namespace /devices) e reage a relay:toggle.
    Reconecta automaticamente quando a conexão cai. Executa em thread separada.
    """
    auth = {"serialNumber": serial_number}
    if auth_token:
        auth["authToken"] = auth_token

    # Mapeamento channel (1-4) -> relayId (ObjectId do ZAccess) para relay:state-update
    relay_id_by_channel: dict[int, str] = {}
    reconnect_delay = RECONNECT_DELAY

    while True:
        sio = socketio.Client(logger=False, engineio_logger=False)

        @sio.event(namespace=NAMESPACE)
        def connect():
            nonlocal reconnect_delay
            reconnect_delay = RECONNECT_DELAY  # reset backoff após conectar
            logger.info("ZAccess: conectado ao servidor %s", server_url)

        @sio.event(namespace=NAMESPACE)
        def connect_error(data):
            logger.warning("ZAccess: erro de conexão - %s", data)

        @sio.event(namespace=NAMESPACE)
        def disconnect():
            logger.warning("ZAccess: desconectado; reconectando em %ss...", reconnect_delay)

        @sio.event(namespace=NAMESPACE)
        def error(data):
            logger.error("ZAccess: erro do servidor - %s", data)

        @sio.on("device:config", namespace=NAMESPACE)
        def device_config(data):
            relay_id_by_channel.clear()
            for r in data.get("relays") or []:
                ch = r.get("channel")
                rid = r.get("id")
                if ch is not None and rid is not None:
                    relay_id_by_channel[int(ch)] = str(rid)
            logger.info("ZAccess: config recebida, relés por canal: %s", relay_id_by_channel)

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

        heartbeat_stop = threading.Event()

        def heartbeat_loop():
            while not heartbeat_stop.is_set():
                heartbeat_stop.wait(timeout=HEARTBEAT_INTERVAL)
                if heartbeat_stop.is_set():
                    break
                if not sio.connected:
                    break
                try:
                    sio.emit("heartbeat", {}, namespace=NAMESPACE)
                except Exception as e:
                    logger.debug("ZAccess: heartbeat falhou - %s", e)

        try:
            sio.connect(
                server_url,
                auth=auth,
                namespaces=[NAMESPACE],
                transports=["websocket", "polling"],
            )
            t = threading.Thread(target=heartbeat_loop, daemon=True)
            t.start()
            sio.wait()
        except Exception as e:
            logger.warning("ZAccess: conexão encerrada - %s", e)
        finally:
            heartbeat_stop.set()
            if sio.connected:
                try:
                    sio.disconnect()
                except Exception:
                    pass

        logger.info("ZAccess: reconectando em %ss...", reconnect_delay)
        time.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX_DELAY)


def start_zaccess_client_in_background(reles: dict) -> threading.Thread | None:
    """
    Inicia o cliente ZAccess em uma thread daemon, se variáveis de ambiente estiverem definidas.
    Retorna a thread ou None se não configurado.
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
        run_zaccess_client(url, serial, reles, auth_token=token)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    logger.info("ZAccess: cliente iniciado em background -> %s", url)
    return thread

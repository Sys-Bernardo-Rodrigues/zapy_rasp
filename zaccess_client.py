"""
Cliente Socket.IO para conectar este dispositivo (zapy) ao servidor ZAccess.
Escuta relay:toggle; envia relay:state-update, input:state-update e heartbeat (telemetria).
A conexão em si é mantida pelo ping nativo do Socket.IO; heartbeat é opcional/complementar.
Ref: https://github.com/Sys-Bernardo-Rodrigues/Projeto-ZAccess
"""
import base64
import os
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import socketio

from face_agent import (
    EventsPoller,
    FaceProvisioningError,
    LocalStore,
    create_face_client,
    enforce_schedule,
    fetch_hikvision_events_since,
    fetch_intelbras_biot_events_since,
    fetch_intelbras_events_since,
)
from face_agent.hikvision_client import HikvisionClient
from face_agent.intelbras_biot_client import IntelbrasBioTClient

logger = logging.getLogger(__name__)

# Versão do zapy_rasp em si (não é firmware de hardware) — mandada no handshake pra
# aparecer na coluna "Firmware" do painel ZAccess em vez do default estático '1.0.0'
# do model (Device.metadata.firmware nunca era atualizado, mostrava sempre o mesmo
# valor não importa o que estivesse rodando de verdade). Bump manual a cada release.
ZAPY_VERSION = "1.0.0"

NAMESPACE = "/devices"
HEARTBEAT_INTERVAL = 30  # telemetria periódica (liveness = ping nativo Socket.IO)
INPUT_PUSH_INTERVAL = 30 # backup: reenvio periódico; mudanças reais são enviadas na hora via callback
FACE_TERMINAL_STATUS_INTERVAL = 60  # heartbeat de saúde dos terminais faciais
SCHEDULE_ENFORCER_INTERVAL = 60     # confere agenda de horário do roster local
FACE_EVENTS_POLL_INTERVAL = 15      # poll de eventos de reconhecimento (face:identified)
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
    auth = {"serialNumber": serial_number, "firmwareVersion": ZAPY_VERSION}
    if auth_token:
        auth["authToken"] = auth_token

    relay_id_by_channel: dict[int, str] = {}
    input_id_by_gpio: dict[int, str] = {}
    face_clients: dict[str, object] = {}  # terminalId -> HikvisionClient/IntelbrasClient
    # Roster com agenda de horário e cursor de poll de eventos — sobrevive a reconexões e
    # a restart do processo (arquivo em disco), pra schedule_enforcer/events_poller
    # funcionarem mesmo com a nuvem fora do ar.
    local_store = LocalStore()
    event_pollers: dict[str, tuple[threading.Thread, threading.Event]] = {}
    reconnect_delay = RECONNECT_DELAY

    while True:
        input_push_stop = threading.Event()
        heartbeat_stop = threading.Event()
        face_status_stop = threading.Event()
        schedule_stop = threading.Event()
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

        def _emit_face_identified(events):
            """Callback dos EventsPoller — log/auditoria, nunca autoriza nada (a decisão de
            abrir já foi tomada localmente pelo terminal). Persiste local primeiro (alimenta o
            painel "Eventos" mesmo se a nuvem estiver fora do ar), só depois tenta emitir."""
            local_store.add_events(events)
            for event in events:
                try:
                    if sio.connected:
                        sio.emit(
                            "face:identified",
                            {
                                "terminalId": event["terminal_id"],
                                "employeeNo": event["employee_no"],
                                "confidence": None,
                                "success": event["success"],
                                "timestamp": event["time"],
                            },
                            namespace=NAMESPACE,
                        )
                except Exception:
                    pass

        def _sync_event_pollers():
            """(Re)inicia um EventsPoller por terminal facial configurado, parando os que
            saíram da config (ex.: terminal removido no painel). Hikvision usa AcsEvent;
            Intelbras XPE só tem doorlog; Intelbras Bio-T/SS usa recordFinder.cgi — três
            protocolos, mesma assimetria do plano de integração facial (seção 6.3),
            refletida aqui na escolha da função de fetch por tipo de client. Os três
            expõem URL de foto por evento (fetch_picture) — Hikvision só em match
            bem-sucedido (minor=75); Bio-T e XPE trazem foto até pra desconhecido."""
            for tid in list(event_pollers.keys()):
                if tid not in face_clients:
                    _, stop_evt = event_pollers.pop(tid)
                    stop_evt.set()

            for tid, client in face_clients.items():
                if tid in event_pollers:
                    continue
                if isinstance(client, HikvisionClient):
                    def fetch(cursor, c=client, t=tid):
                        events, next_cursor = fetch_hikvision_events_since(c, t, cursor)
                        # Baixa a foto capturada na hora (pictureURL só vem em match bem-sucedido)
                        # antes de devolver — assim já entra persistida no primeiro add_events.
                        for event in events:
                            url = event.get("picture_url")
                            event["picture"] = c.fetch_picture(url) if url else None
                        return events, next_cursor
                elif isinstance(client, IntelbrasBioTClient):
                    def fetch(cursor, c=client, t=tid):
                        events, next_cursor = fetch_intelbras_biot_events_since(c, t, cursor)
                        for event in events:
                            url = event.get("picture_url")
                            event["picture"] = c.fetch_picture(url) if url else None
                        return events, next_cursor
                else:
                    # Direção fixa "unknown": FaceTerminal ainda não tem esse campo no
                    # servidor (sem consumidor até schedule_enforcer/events_poller existirem).
                    def fetch(cursor, c=client, t=tid):
                        events, next_cursor = fetch_intelbras_events_since(c, t, "unknown", cursor)
                        # Baixa a foto do doorlog (campo Picture, vem até pra desconhecido)
                        # antes de devolver — assim já entra persistida no primeiro add_events.
                        for event in events:
                            url = event.get("picture_url")
                            event["picture"] = c.fetch_picture(url) if url else None
                        return events, next_cursor
                poller = EventsPoller(local_store, tid, fetch)
                thread, stop_evt = poller.start(_emit_face_identified, interval_seconds=FACE_EVENTS_POLL_INTERVAL)
                event_pollers[tid] = (thread, stop_evt)

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

            face_clients.clear()
            for t in data.get("faceTerminals") or []:
                tid = t.get("id")
                if not tid:
                    continue
                if t.get("name"):
                    # Nome vem do ZAccess (id do ZAccess, não o de face_terminals_store.py) —
                    # persiste local pro painel de Eventos conseguir exibir em vez do id cru.
                    local_store.set_terminal_name(str(tid), t["name"])
                try:
                    face_clients[str(tid)] = create_face_client(
                        t["vendor"], host=t["host"], port=t.get("port") or 80,
                        username=t["username"], password=t["password"],
                        https=bool(t.get("https")), verify_tls=t.get("rejectUnauthorized", True) is not False,
                        relay_level=t.get("relayLevel") or 0,
                    )
                except Exception as e:
                    logger.error("ZAccess: falha ao configurar terminal facial %s - %s", tid, e)

            _sync_event_pollers()

            logger.info(
                "ZAccess: config recebida, relés: %s, inputs: %s, terminais faciais: %s",
                list(relay_id_by_channel.keys()), list(input_id_by_gpio.keys()), list(face_clients.keys()),
            )
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

        def _emit_enroll_ack(person_id: str, terminal_id: str, status: str, error: str | None = None):
            try:
                if sio.connected:
                    payload = {"personId": person_id, "terminalId": terminal_id, "status": status}
                    if error:
                        payload["error"] = error
                    sio.emit("face:enroll-ack", payload, namespace=NAMESPACE)
            except Exception:
                pass

        def _emit_card_ack(person_id: str, terminal_id: str, card_no: str, status: str, error: str | None = None):
            """Canal próprio (card:enroll-ack) — face e cartão são credenciais
            independentes no servidor (cardEnrollmentStatus separado de
            faceEnrollmentStatus), não dá pra reaproveitar face:enroll-ack aqui. Carrega
            cardNo porque uma pessoa pode ter mais de um cartão — sem isso o servidor não
            sabe a qual cartão o ack se refere."""
            try:
                if sio.connected:
                    payload = {"personId": person_id, "terminalId": terminal_id, "cardNo": card_no, "status": status}
                    if error:
                        payload["error"] = error
                    sio.emit("card:enroll-ack", payload, namespace=NAMESPACE)
            except Exception:
                pass

        @sio.on("face:enroll", namespace=NAMESPACE)
        def face_enroll(data):
            """Servidor pede pra cadastrar um rosto num terminal. Roda em thread separada
            pra não travar o processamento de outros eventos socket (enroll faz 2 chamadas
            HTTP ao terminal, pode levar alguns segundos)."""
            person_id = str(data.get("personId") or "")
            employee_no = str(data.get("employeeNo") or person_id)
            terminal_id = str(data.get("terminalId") or "")
            name = data.get("name") or employee_no
            jpeg_b64 = data.get("jpegBase64")
            if not person_id or not terminal_id or not jpeg_b64:
                logger.warning("ZAccess: face:enroll inválido - %s", {k: v for k, v in data.items() if k != "jpegBase64"})
                return

            client = face_clients.get(terminal_id)
            if not client:
                logger.error("ZAccess: face:enroll pra terminal desconhecido %s", terminal_id)
                # Em thread separada: emitir a partir do próprio callback de recepção do
                # evento trava o ack silenciosamente (reentrância no cliente socketio,
                # validado ao vivo — sem thread, o emit nunca chega no servidor).
                threading.Thread(target=_emit_enroll_ack, args=(person_id, terminal_id, "failed", "terminal não configurado neste zapy"), daemon=True).start()
                return

            def run():
                try:
                    jpeg = base64.b64decode(jpeg_b64)
                    client.enroll_face(employee_no, name, jpeg)
                    # Guarda no roster local (com a foto e a agenda) pra schedule_enforcer
                    # poder reaplicar sozinho, sem depender da nuvem estar no ar.
                    local_store.upsert_roster(terminal_id, employee_no, name, jpeg, data.get("accessSchedule"))
                    local_store.set_enrolled(terminal_id, employee_no, True)
                    logger.info("ZAccess: rosto de %s cadastrado no terminal %s", name, terminal_id)
                    _emit_enroll_ack(person_id, terminal_id, "enrolled")
                except Exception as e:
                    logger.error("ZAccess: falha ao cadastrar rosto de %s no terminal %s - %s", name, terminal_id, e)
                    _emit_enroll_ack(person_id, terminal_id, "failed", str(e))

            threading.Thread(target=run, daemon=True).start()

        @sio.on("face:revoke", namespace=NAMESPACE)
        def face_revoke(data):
            """Servidor pede pra remover um rosto de um terminal."""
            person_id = str(data.get("personId") or "")
            employee_no = str(data.get("employeeNo") or person_id)
            terminal_id = str(data.get("terminalId") or "")
            if not person_id or not terminal_id:
                logger.warning("ZAccess: face:revoke inválido - %s", data)
                return

            client = face_clients.get(terminal_id)
            if not client:
                logger.error("ZAccess: face:revoke pra terminal desconhecido %s", terminal_id)
                threading.Thread(target=_emit_enroll_ack, args=(person_id, terminal_id, "failed", "terminal não configurado neste zapy"), daemon=True).start()
                return

            def run():
                try:
                    client.delete_user_info(employee_no)
                    # Revoke explícito do servidor = remove do roster de vez (diferente do
                    # schedule_enforcer, que só desmarca "enrolled" pra poder reaplicar depois).
                    local_store.remove_roster(terminal_id, employee_no)
                    logger.info("ZAccess: rosto de %s removido do terminal %s", employee_no, terminal_id)
                    _emit_enroll_ack(person_id, terminal_id, "revoked")
                except Exception as e:
                    logger.error("ZAccess: falha ao remover rosto de %s do terminal %s - %s", employee_no, terminal_id, e)
                    _emit_enroll_ack(person_id, terminal_id, "failed", str(e))

            threading.Thread(target=run, daemon=True).start()

        @sio.on("card:enroll", namespace=NAMESPACE)
        def card_enroll(data):
            """Servidor pede pra vincular um cartão a um terminal facial. Mesmo padrão do
            face:enroll — roda em thread separada, ack num canal próprio (card:enroll-ack)."""
            person_id = str(data.get("personId") or "")
            employee_no = str(data.get("employeeNo") or person_id)
            terminal_id = str(data.get("terminalId") or "")
            name = data.get("name") or employee_no
            card_no = data.get("cardNo")
            if not person_id or not terminal_id or not card_no:
                logger.warning("ZAccess: card:enroll inválido - %s", data)
                return

            client = face_clients.get(terminal_id)
            if not client:
                logger.error("ZAccess: card:enroll pra terminal desconhecido %s", terminal_id)
                threading.Thread(target=_emit_card_ack, args=(person_id, terminal_id, card_no, "failed", "terminal não configurado neste zapy"), daemon=True).start()
                return

            def run():
                try:
                    client.enroll_card(employee_no, name, card_no)
                    # Guarda no roster local de cartão pra clear-all/resync poder
                    # reaplicar sozinho depois, mesmo racional do roster de face.
                    local_store.upsert_card_roster(terminal_id, employee_no, name, card_no)
                    local_store.set_card_enrolled(terminal_id, employee_no, card_no, True)
                    logger.info("ZAccess: cartão de %s cadastrado no terminal %s", name, terminal_id)
                    _emit_card_ack(person_id, terminal_id, card_no, "enrolled")
                except Exception as e:
                    logger.error("ZAccess: falha ao cadastrar cartão de %s no terminal %s - %s", name, terminal_id, e)
                    _emit_card_ack(person_id, terminal_id, card_no, "failed", str(e))

            threading.Thread(target=run, daemon=True).start()

        @sio.on("card:revoke", namespace=NAMESPACE)
        def card_revoke(data):
            """Servidor pede pra remover um cartão específico de um terminal facial —
            pessoa pode ter mais de um, cardNo é obrigatório pra saber qual."""
            person_id = str(data.get("personId") or "")
            employee_no = str(data.get("employeeNo") or person_id)
            terminal_id = str(data.get("terminalId") or "")
            card_no = data.get("cardNo")
            if not person_id or not terminal_id or not card_no:
                logger.warning("ZAccess: card:revoke inválido - %s", data)
                return

            client = face_clients.get(terminal_id)
            if not client:
                logger.error("ZAccess: card:revoke pra terminal desconhecido %s", terminal_id)
                threading.Thread(target=_emit_card_ack, args=(person_id, terminal_id, card_no, "failed", "terminal não configurado neste zapy"), daemon=True).start()
                return

            def run():
                try:
                    client.delete_card(employee_no, card_no)
                    local_store.remove_card_roster(terminal_id, employee_no, card_no)
                    logger.info("ZAccess: cartão de %s removido do terminal %s", employee_no, terminal_id)
                    _emit_card_ack(person_id, terminal_id, card_no, "revoked")
                except Exception as e:
                    logger.error("ZAccess: falha ao remover cartão de %s do terminal %s - %s", employee_no, terminal_id, e)
                    _emit_card_ack(person_id, terminal_id, card_no, "failed", str(e))

            threading.Thread(target=run, daemon=True).start()

        def _clear_and_resync_terminal(terminal_id: str, client) -> dict:
            """Zera o terminal (clear_all_users) e ressincroniza a partir do roster local
            (face + cartão) — devolve o device ao estado que o ZAccess já espera, sem
            precisar reenviar foto/cartão de cada pessoa manualmente de novo. Cada entrada
            falha isoladamente (uma pessoa com problema não trava o resto do resync)."""
            if not hasattr(client, "clear_all_users"):
                raise FaceProvisioningError("clear_all_users não implementado para esse vendor")
            client.clear_all_users()

            faces_ok = faces_failed = cards_ok = cards_failed = 0
            for entry in local_store.list_by_terminal(terminal_id):
                try:
                    client.enroll_face(entry.employee_no, entry.name, entry.jpeg)
                    faces_ok += 1
                except Exception as e:
                    logger.error("ZAccess: resync falhou pra face de %s no terminal %s - %s", entry.employee_no, terminal_id, e)
                    faces_failed += 1
            for entry in local_store.list_card_by_terminal(terminal_id):
                try:
                    client.enroll_card(entry.employee_no, entry.name, entry.card_no)
                    cards_ok += 1
                except Exception as e:
                    logger.error("ZAccess: resync falhou pro cartão de %s no terminal %s - %s", entry.employee_no, terminal_id, e)
                    cards_failed += 1
            return {"facesResynced": faces_ok, "facesFailed": faces_failed, "cardsResynced": cards_ok, "cardsFailed": cards_failed}

        def _emit_clear_all_ack(terminal_id: str, status: str, error: str | None = None, counts: dict | None = None):
            try:
                if sio.connected:
                    payload = {"terminalId": terminal_id, "status": status}
                    if error:
                        payload["error"] = error
                    if counts:
                        payload.update(counts)
                    sio.emit("face:clear-all-ack", payload, namespace=NAMESPACE)
            except Exception:
                pass

        @sio.on("face:clear-all", namespace=NAMESPACE)
        def face_clear_all(data):
            """Servidor pede pra zerar um terminal e ressincronizar do roster local
            (face + cartão) — ex.: terminal trocado/resetado em campo, ou saiu de
            sincronia com o que o ZAccess acha que está cadastrado."""
            terminal_id = str(data.get("terminalId") or "")
            if not terminal_id:
                logger.warning("ZAccess: face:clear-all inválido - %s", data)
                return

            client = face_clients.get(terminal_id)
            if not client:
                logger.error("ZAccess: face:clear-all pra terminal desconhecido %s", terminal_id)
                threading.Thread(target=_emit_clear_all_ack, args=(terminal_id, "failed", "terminal não configurado neste zapy"), daemon=True).start()
                return

            def run():
                try:
                    counts = _clear_and_resync_terminal(terminal_id, client)
                    logger.info("ZAccess: terminal %s zerado e ressincronizado -> %s", terminal_id, counts)
                    _emit_clear_all_ack(terminal_id, "done", None, counts)
                except Exception as e:
                    logger.error("ZAccess: falha ao zerar/ressincronizar terminal %s - %s", terminal_id, e)
                    _emit_clear_all_ack(terminal_id, "failed", str(e))

            threading.Thread(target=run, daemon=True).start()

        @sio.on("face:open-door", namespace=NAMESPACE)
        def face_open_door(data):
            """Servidor pede abertura remota da porta vinculada a um terminal facial —
            comando administrativo direto, não passa pelo reconhecimento facial."""
            terminal_id = str(data.get("terminalId") or "")
            by_app = data.get("byApp")
            by_user_id = data.get("byUserId")
            by_location_user_id = data.get("byLocationUserId")
            client = face_clients.get(terminal_id)
            if not client:
                logger.error("ZAccess: face:open-door pra terminal desconhecido %s", terminal_id)
                return

            def run():
                result = client.open_door()
                try:
                    if sio.connected:
                        payload = {"terminalId": terminal_id, "status": "opened" if result.get("ok") else "failed"}
                        if not result.get("ok"):
                            payload["error"] = result.get("reason")
                        # Ecoa quem pediu (só o servidor sabe, veio no evento) — o handler do
                        # ack usa isso pra logar com o nome de quem abriu (byApp) e popular a
                        # referência de verdade no ActivityLog (byUserId/byLocationUserId), em
                        # vez de genérico.
                        if by_app:
                            payload["byApp"] = by_app
                        if by_user_id:
                            payload["byUserId"] = by_user_id
                        if by_location_user_id:
                            payload["byLocationUserId"] = by_location_user_id
                        sio.emit("face:open-door-ack", payload, namespace=NAMESPACE)
                except Exception:
                    pass
                logger.info("ZAccess: abertura remota do terminal %s -> %s", terminal_id, result)

                if result.get("ok"):
                    # Mesmo tratamento do cockpit (app.py) — sem isso a abertura pedida pelo
                    # ZAccess (painel ou app) fica invisível no /eventos local: não passa por
                    # reconhecimento, não gera AcsEvent/doorlog nenhum pra poller pegar.
                    now = datetime.now(timezone(timedelta(hours=-3)))
                    capture_snapshot = getattr(client, "capture_snapshot", None)
                    label = f"Liberado via ZAccess ({by_app})" if by_app else "Liberado via ZAccess"
                    local_store.add_events([{
                        "dedupe_key": f"{terminal_id}:zaccess-open:{now.strftime('%Y%m%dT%H%M%S%f')}",
                        "terminal_id": terminal_id, "employee_no": None, "time": now.isoformat(),
                        "direction": "unknown", "success": True, "source": "zaccess",
                        "picture": capture_snapshot() if capture_snapshot else None,
                        "device_name": label,
                    }])

            threading.Thread(target=run, daemon=True).start()

        @sio.on("face:reboot", namespace=NAMESPACE)
        def face_reboot(data):
            """Servidor pede reboot físico de um terminal facial."""
            terminal_id = str(data.get("terminalId") or "")
            client = face_clients.get(terminal_id)
            if not client:
                logger.error("ZAccess: face:reboot pra terminal desconhecido %s", terminal_id)
                return

            def run():
                result = client.reboot_terminal()
                try:
                    if sio.connected:
                        payload = {"terminalId": terminal_id, "status": "rebooted" if result.get("ok") else "failed"}
                        if not result.get("ok"):
                            payload["error"] = result.get("reason")
                        sio.emit("face:reboot-ack", payload, namespace=NAMESPACE)
                except Exception:
                    pass
                logger.info("ZAccess: reboot do terminal %s -> %s", terminal_id, result)

            threading.Thread(target=run, daemon=True).start()

        def face_terminal_status_loop():
            """Heartbeat de saúde dos terminais faciais — só os que têm check_health (Intelbras
            hoje; Hikvision também expõe via System/deviceInfo)."""
            while not face_status_stop.is_set():
                if face_status_stop.wait(timeout=FACE_TERMINAL_STATUS_INTERVAL):
                    break
                if not sio.connected:
                    continue
                for terminal_id, client in list(face_clients.items()):
                    if not hasattr(client, "check_health"):
                        continue
                    try:
                        client.check_health()
                        status = "online"
                    except Exception:
                        status = "offline"
                    try:
                        sio.emit(
                            "face:terminal-status",
                            {"terminalId": terminal_id, "status": status, "lastSeen": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
                            namespace=NAMESPACE,
                        )
                    except Exception:
                        pass

        def schedule_enforcer_loop():
            """Aplica a agenda de horário do roster local — funciona mesmo com a nuvem fora
            do ar, porque só depende do que já foi persistido em enroll (ver face_enroll)."""
            while not schedule_stop.is_set():
                if schedule_stop.wait(timeout=SCHEDULE_ENFORCER_INTERVAL):
                    break
                for terminal_id, client in list(face_clients.items()):
                    try:
                        enforce_schedule(client, local_store, terminal_id, terminal_id)
                    except Exception:
                        logger.exception("ZAccess: falha no schedule_enforcer do terminal %s", terminal_id)

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
            t_face = threading.Thread(target=face_terminal_status_loop, daemon=True)
            t_face.start()
            t_schedule = threading.Thread(target=schedule_enforcer_loop, daemon=True)
            t_schedule.start()
            sio.wait()
        except Exception as e:
            logger.warning("ZAccess: conexão encerrada - %s", e)
        finally:
            heartbeat_stop.set()
            input_push_stop.set()
            face_status_stop.set()
            schedule_stop.set()
            for _, stop_evt in event_pollers.values():
                stop_evt.set()
            event_pollers.clear()
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

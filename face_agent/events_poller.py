"""
Lê eventos de reconhecimento facial dos terminais (poll) e normaliza num
formato único, independente de fabricante — pra reportar via `face:identified`
(log/auditoria, nunca autoriza nada — a decisão de abrir já foi tomada
localmente pelo terminal). Porta de agente-local/src/isapi/{acsEvents,
eventParser}.ts e src/intelbras/doorLog.ts.

Assimetria real entre os dois fabricantes (documentada no plano de integração
facial, seção 6.3): Hikvision tem poll via AcsEvent; Intelbras só tem poll via
doorlog/get (sem push) — por isso duas funções de fetch, uma por vendor, mas
um único formato de evento normalizado na saída.
"""
import logging
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

from .errors import FaceProvisioningError
from .local_store import PollCursor

logger = logging.getLogger(__name__)

# minor code 75 = reconhecimento facial bem-sucedido (76 = malsucedido) — mesma
# convenção documentada no eventParser.ts do z-edu pra terminais Hikvision.
_SUCCESS_MINOR_CODES = {75}
_FAILURE_MINOR_CODES = {76}
# major=5 tem MUITOS outros minor codes no mesmo stream de AcsEvent (ex.: 21/22 =
# contato de porta abriu/fechou) — validado ao vivo contra um DS-K1T671MF-L real
# (search em AcsEvent traz esses eventos misturados com os de reconhecimento facial).
# Sem esse filtro, cada abertura/fechamento de porta vira uma linha "Negado" falsa
# no painel de Eventos.
_FACE_MINOR_CODES = _SUCCESS_MINOR_CODES | _FAILURE_MINOR_CODES


def _find_field_ci(obj, name: str):
    """Busca um campo por nome, case-insensitive, em qualquer profundidade —
    necessário porque o nome do campo de sub-evento varia por firmware/modelo.
    BFS (não DFS): um achado mais raso sempre vence um mais fundo."""
    lower = name.lower()
    queue = deque([obj])
    seen = set()
    while queue:
        cur = queue.popleft()
        if not isinstance(cur, (dict, list)):
            continue
        cur_id = id(cur)
        if cur_id in seen:
            continue
        seen.add(cur_id)
        if isinstance(cur, list):
            queue.extend(cur)
            continue
        for key, value in cur.items():
            if key.lower() == lower:
                return value
        for value in cur.values():
            if isinstance(value, (dict, list)):
                queue.append(value)
    return None


def _normalize_direction(v) -> str:
    if not isinstance(v, str):
        return "unknown"
    s = v.lower()
    if "in" in s or s in ("entry", "entrance"):
        return "in"
    if "out" in s or s == "exit":
        return "out"
    return "unknown"


def _normalize_event(terminal_id: str, obj, source: str) -> Optional[dict]:
    """Normaliza um objeto de evento cru (já parseado de JSON, de poll) num
    formato único. Retorna None quando o evento não deve virar uma passagem:
    eventState inativo (heartbeat, não é falha) ou payload sem campos mínimos."""
    if not isinstance(obj, dict):
        return None

    event_state = _find_field_ci(obj, "eventState")
    if isinstance(event_state, str) and event_state.lower() != "active":
        return None

    major_raw = _find_field_ci(obj, "majorEventType")
    if major_raw is None:
        major_raw = _find_field_ci(obj, "major")
    minor_raw = _find_field_ci(obj, "minorEventType")
    if minor_raw is None:
        minor_raw = _find_field_ci(obj, "subEventType")
    if minor_raw is None:
        minor_raw = _find_field_ci(obj, "minor")
    major = int(major_raw) if major_raw not in (None, "") else None
    minor = int(minor_raw) if minor_raw not in (None, "") else None
    if minor not in _FACE_MINOR_CODES:
        return None

    employee_no_raw = _find_field_ci(obj, "employeeNoString")
    if employee_no_raw is None:
        employee_no_raw = _find_field_ci(obj, "employeeNo")
    employee_no = str(employee_no_raw) if employee_no_raw not in (None, "") else None

    # Nome que o próprio terminal já manda no evento (cardholder name do Hikvision, ex.:
    # "Bernardo (Teste)") — validado ao vivo contra um DS-K1T671MF-L real. Único jeito de
    # mostrar um nome pra quem foi enrolado direto no device, fora do fluxo do ZAccess
    # (por isso não está no roster local e o join por lá não acha nada).
    device_name_raw = _find_field_ci(obj, "name")
    device_name = str(device_name_raw) if isinstance(device_name_raw, str) and device_name_raw.strip() else None

    time_raw = _find_field_ci(obj, "dateTime")
    if time_raw is None:
        time_raw = _find_field_ci(obj, "time")
    time = time_raw if isinstance(time_raw, str) and time_raw else datetime.now(timezone.utc).isoformat()

    dir_raw = _find_field_ci(obj, "direction")
    if dir_raw is None:
        dir_raw = _find_field_ci(obj, "attendanceStatus")
    direction = _normalize_direction(dir_raw)

    serial_no = _find_field_ci(obj, "serialNo")
    dedupe_key = ":".join([
        terminal_id, str(major) if major is not None else "x", str(minor) if minor is not None else "x",
        time, employee_no or (str(serial_no) if serial_no else "unknown"),
    ])

    success = minor in _SUCCESS_MINOR_CODES

    # URL completa (fora do namespace /ISAPI/, mesmo host) da foto capturada na hora do
    # reconhecimento — só presente em eventos minor=75 (match bem-sucedido). Baixada depois
    # por quem chama (precisa do client/credenciais, que _normalize_event não tem).
    picture_url_raw = _find_field_ci(obj, "pictureURL")
    picture_url = picture_url_raw if isinstance(picture_url_raw, str) and picture_url_raw else None

    return {
        "dedupe_key": dedupe_key, "terminal_id": terminal_id, "employee_no": employee_no,
        "time": time, "direction": direction, "major_event_type": major, "minor_event_type": minor,
        "success": success, "source": source, "picture_url": picture_url, "device_name": device_name, "raw": obj,
    }


# --- Hikvision: poll via AcsEvent ---

_PAGE_SIZE = 30
_NO_MATCH_TOKENS = {"no_matches", "no match", "nomatch", "nomatches"}
_MORE_TOKENS = {"more"}


def get_device_time(client) -> str:
    """Horário do PRÓPRIO terminal (não do agente) — o device pode estar com o
    relógio dessincronizado sem que isso quebre a autenticação; buscar eventos
    usando o relógio do agente faz a busca sumir silenciosamente (o device
    compara contra o relógio dele)."""
    res = client.request("GET", "System/time")
    if not (200 <= res.status_code < 300):
        raise FaceProvisioningError(f"falha ao consultar System/time (status {res.status_code}): {res.text}")
    try:
        body = res.json()
    except ValueError:
        body = None
    local_time = _find_field_ci(body, "localTime") if body is not None else None
    if isinstance(local_time, str) and local_time:
        return local_time
    match = re.search(r"<localTime>([^<]+)</localTime>", res.text or "", re.IGNORECASE)
    if match:
        return match.group(1)
    raise FaceProvisioningError(f"System/time sem campo localTime válido: {res.text}")


def fetch_hikvision_events_since(client, terminal_id: str, cursor: PollCursor) -> tuple[list[dict], PollCursor]:
    """Busca eventos novos via POST /AccessControl/AcsEvent — paginado, nunca
    reprocessa eventos já vistos (filtra por cursor.last_event_time)."""
    search_id = str(uuid.uuid4())
    device_now = get_device_time(client)
    start_time = cursor.last_event_time or device_now
    end_time = device_now

    position = 0
    collected: list[dict] = []
    # Só rastreia eventos NOVOS encontrados neste ciclo (nunca herda cursor.last_event_time)
    # — se nada for encontrado, cai no fallback `or end_time` abaixo. Sem isso, sem eventos
    # novos, o cursor nunca avança e a janela de busca fica com largura zero pra sempre
    # (mesmo bug documentado no z-edu, HikvisionAcsEventFetcher.fetchSince).
    latest_time: Optional[str] = None

    while True:
        res = client.request("POST", "AccessControl/AcsEvent", json_body={
            "AcsEventCond": {
                "searchID": search_id, "searchResultPosition": position, "maxResults": _PAGE_SIZE,
                "major": 0, "minor": 0, "startTime": start_time, "endTime": end_time,
            },
        })
        if not (200 <= res.status_code < 300):
            raise FaceProvisioningError(f"falha ao buscar AcsEvent (status {res.status_code}): {res.text}")
        try:
            body = res.json()
        except ValueError:
            body = None

        root = (body.get("AcsEvent") if isinstance(body, dict) else None) or body
        status_raw = _find_field_ci(root, "responseStatusStrg")
        if status_raw is None:
            status_raw = _find_field_ci(root, "responseStatus")
        status = status_raw.strip().lower() if isinstance(status_raw, str) else ""

        info_list_raw = _find_field_ci(root, "InfoList")
        if isinstance(info_list_raw, list):
            items = info_list_raw
        elif info_list_raw:
            items = [info_list_raw]
        else:
            items = []

        for item in items:
            event = _normalize_event(terminal_id, item, "poll")
            if event is None:
                continue
            if cursor.last_event_time and event["time"] <= cursor.last_event_time:
                continue
            collected.append(event)
            if latest_time is None or event["time"] > latest_time:
                latest_time = event["time"]

        if len(items) == 0 or status in _NO_MATCH_TOKENS:
            break
        if status in _MORE_TOKENS:
            position += len(items)
            continue
        break  # status "OK" (ou token desconhecido, tratado como última página por segurança)

    collected.sort(key=lambda e: e["time"])
    next_cursor = PollCursor(terminal_id=terminal_id, last_event_time=latest_time or end_time, search_id=search_id)
    return collected, next_cursor


# --- Intelbras: poll via doorlog/get (sem push) ---

_DOORLOG_FETCH_NUM = 200


def fetch_intelbras_events_since(client, terminal_id: str, direction: str, cursor: PollCursor) -> tuple[list[dict], PollCursor]:
    """Busca eventos novos via doorlog/get — o endpoint não pagina por ID, só
    retorna os N últimos; cobrimos com janela generosa e filtramos client-side
    pelo ID (sequencial, único por terminal) maior que o cursor."""
    res = client.call("doorlog", "get", {"Num": str(_DOORLOG_FETCH_NUM)})
    if res.get("retcode") != 0:
        raise FaceProvisioningError(f"doorlog/get retornou retcode {res.get('retcode')}: {res.get('message')}")

    items = [i for i in ((res.get("data") or {}).get("item") or []) if isinstance(i.get("ID"), str)]
    last_seen_id = int(cursor.search_id) if cursor.search_id is not None else None

    if last_seen_id is None:
        # Primeira execução (sem cursor persistido): não enfileira o histórico acumulado,
        # só ancora o cursor no maior ID já existente — não inunda a fila no boot do agente.
        max_id = max((int(i["ID"]) for i in items), default=0)
        return [], PollCursor(terminal_id=terminal_id, last_event_time=cursor.last_event_time, search_id=str(max_id))

    new_items = sorted((i for i in items if int(i["ID"]) > last_seen_id), key=lambda i: int(i["ID"]))
    events = [e for e in (_intelbras_doorlog_to_event(terminal_id, direction, i) for i in new_items) if e is not None]
    max_id = max([int(i["ID"]) for i in new_items], default=last_seen_id)

    next_cursor = PollCursor(
        terminal_id=terminal_id,
        last_event_time=events[-1]["time"] if events else cursor.last_event_time,
        search_id=str(max_id),
    )
    return events, next_cursor


def _intelbras_doorlog_to_event(terminal_id: str, direction: str, item: dict) -> Optional[dict]:
    if not item.get("ID") or not item.get("Date") or not item.get("Time"):
        return None
    success = item.get("Status") == "Success"
    # UserID vem como a string literal "Desconhecido" (não um employeeNo real) quando o
    # reconhecimento falha — validado ao vivo contra XPE-3200-PLUS-IP.
    user_id = item.get("UserID")
    employee_no = user_id if success and user_id and user_id != "Desconhecido" else None
    # Firmware não embute timezone no doorlog — fixo -03:00 (mesmo racional do z-edu:
    # só opera no Brasil por ora).
    time = f"{item['Date']}T{item['Time']}-03:00"
    return {
        "dedupe_key": f"{terminal_id}:doorlog:{item['ID']}", "terminal_id": terminal_id,
        "employee_no": employee_no, "time": time, "direction": direction,
        "major_event_type": None, "minor_event_type": None, "success": success,
        "source": "poll", "raw": item,
    }


# --- Intelbras Bio-T/SS: poll via recordFinder.cgi (AccessControlCardRec) ---

_BIOT_POLL_WINDOW_SECONDS = 300  # janela de busca por ciclo, bem maior que os 15s do
# poll — cobre atraso/falha de um ciclo sem perder evento; overlap é inofensivo porque
# a dedupe é por RecNo, não pela janela de tempo.
_BRT = timezone(timedelta(hours=-3))  # mesmo racional do doorlog do XPE: só opera no Brasil por ora


def fetch_intelbras_biot_events_since(client, terminal_id: str, cursor: PollCursor) -> tuple[list[dict], PollCursor]:
    """Busca eventos novos via recordFinder.cgi (AccessControlCardRec) — janela de tempo
    (StartTime/EndTime, epoch), dedupe client-side por RecNo (sequencial, único por
    terminal) maior que o cursor, mesmo racional do doorlog/get do XPE (fetch_intelbras_events_since).
    Sem endpoint dedicado a evento facial (diferente do doorlog do XPE): AccessControlCardRec
    mistura face/cartão/senha no mesmo log — sem legenda documentada pro campo Method, não
    dá pra filtrar só facial com segurança, então todo acesso registrado entra.
    ponytail: revisitar o filtro por método se a doc um dia publicar a tabela de Method."""
    now = int(time.time())
    items = client.fetch_access_records(now - _BIOT_POLL_WINDOW_SECONDS, now)
    last_seen_recno = int(cursor.search_id) if cursor.search_id is not None else None

    if last_seen_recno is None:
        # Primeira execução (sem cursor persistido): não enfileira o que já está na janela,
        # só ancora o cursor no maior RecNo visto — não inunda a fila no boot do agente.
        max_recno = max((int(i["RecNo"]) for i in items if i.get("RecNo")), default=0)
        return [], PollCursor(terminal_id=terminal_id, last_event_time=cursor.last_event_time, search_id=str(max_recno))

    new_items = sorted((i for i in items if i.get("RecNo") and int(i["RecNo"]) > last_seen_recno), key=lambda i: int(i["RecNo"]))
    events = [e for e in (_biot_record_to_event(terminal_id, i) for i in new_items) if e is not None]
    max_recno = max([int(i["RecNo"]) for i in new_items], default=last_seen_recno)

    next_cursor = PollCursor(
        terminal_id=terminal_id,
        last_event_time=events[-1]["time"] if events else cursor.last_event_time,
        search_id=str(max_recno),
    )
    return events, next_cursor


def _biot_record_to_event(terminal_id: str, item: dict) -> Optional[dict]:
    rec_no = item.get("RecNo")
    create_time = item.get("CreateTime")
    if not rec_no or not create_time:
        return None
    try:
        epoch = int(create_time)
    except ValueError:
        return None
    time_str = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(_BRT).isoformat()
    # ErrorCode "0" = sem erro, validado contra os exemplos da doc oficial (Status=1 +
    # ErrorCode=0 em acesso liberado; Status=0 + ErrorCode=16 em acesso negado).
    success = item.get("ErrorCode") == "0"
    user_id = (item.get("UserID") or "").strip()
    employee_no = user_id if success and user_id else None
    picture_path = (item.get("URL") or "").strip() or None
    return {
        "dedupe_key": f"{terminal_id}:biot:{rec_no}", "terminal_id": terminal_id,
        "employee_no": employee_no, "time": time_str, "direction": _normalize_direction(item.get("Type")),
        "major_event_type": None, "minor_event_type": None, "success": success,
        "source": "poll", "picture_url": picture_path, "raw": item,
    }


# --- loop de poll por terminal ---

class EventsPoller:
    """Cuida do poll de eventos de UM terminal — persiste o cursor no
    LocalStore entre ciclos (sobrevive a restart do processo). `fetch` é uma
    das duas funções acima, já com client/terminal_id/direction fechados via
    closure/partial pelo chamador."""

    def __init__(self, store, terminal_id: str, fetch):
        self._store = store
        self._terminal_id = terminal_id
        self._fetch = fetch  # (cursor: PollCursor) -> (events, next_cursor)

    def poll_once(self) -> list[dict]:
        cursor = self._store.get_cursor(self._terminal_id) or PollCursor(terminal_id=self._terminal_id)
        events, next_cursor = self._fetch(cursor)
        self._store.set_cursor(next_cursor)
        return events

    def start(self, on_events, *, interval_seconds: float = 10.0):
        """Roda poll_once em loop numa thread daemon, chamando on_events(events)
        a cada ciclo com o que for novo. Retorna (thread, stop_event) — quem
        chama guarda o stop_event e faz .set() nele pra parar a thread."""
        stop = threading.Event()

        def run():
            while not stop.is_set():
                try:
                    events = self.poll_once()
                    if events:
                        on_events(events)
                except Exception:
                    logger.exception("events_poller (%s): falha no ciclo de poll", self._terminal_id)
                stop.wait(timeout=interval_seconds)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, stop

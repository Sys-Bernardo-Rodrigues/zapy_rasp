import csv
import io
import os
import logging
import secrets
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import Flask, render_template, redirect, url_for, request, jsonify, session, Response
from functools import wraps

load_dotenv()
from gpiozero import OutputDevice, DigitalInputDevice

from zaccess_client import start_zaccess_client_in_background, submit_controlid_event
from config_env import read_config, get_config_for_display, write_config
import face_terminals_store
import vehicle_antennas_store
import users_store
from face_agent import FaceProvisioningError, LocalStore, create_face_client
from face_agent.controlid_uhf_client import ControlIdTerminal, ControlIdUhfClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

def _load_or_create_secret_key() -> str:
    """Sem valor padrão fixo de propósito: um literal hardcoded aqui vira uma chave
    conhecida publicamente (está no repositório) — qualquer instalação que suba sem
    ZAPY_SECRET_KEY setado ficaria assinando cookie de sessão com essa string, permitindo
    forjar um cookie de admin sem nunca logar (ex.: flask-unsign). Gera uma chave aleatória
    na primeira vez e persiste num arquivo local (fora do git) — reinícios seguintes reusam
    a mesma, sem precisar o instalador editar nada manualmente."""
    env_key = os.environ.get("ZAPY_SECRET_KEY", "").strip()
    if env_key:
        return env_key
    key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret_key")
    if os.path.isfile(key_path):
        with open(key_path, "r", encoding="utf-8") as f:
            existing = f.read().strip()
        if existing:
            return existing
    new_key = secrets.token_hex(32)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new_key)
    return new_key


app = Flask(__name__, template_folder='painel_rele/templates')
app.secret_key = _load_or_create_secret_key()

# Mesmo arquivo SQLite (WAL) que zaccess_client.py escreve — conexão própria só de
# leitura pro painel, não depende da thread do cliente ZAccess estar rodando.
local_store = LocalStore()

EVENT_DIRECTION_LABEL = {"in": "Entrada", "out": "Saída", "unknown": "—"}


def login_required(fn):
    """Qualquer usuário logado — admin ou porteiro (ver ROLES em users_store.py)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)

    return wrapper


def admin_required(fn):
    """Só admin. Porteiro logado cai no cockpit em vez de tomar 403 cru — ele só não
    enxerga rota nenhuma dessas no menu, então isso só protege contra digitar a URL."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "admin":
            return redirect(url_for("cockpit_page"))
        return fn(*args, **kwargs)

    return wrapper

# Configuração dos Relés (Pinos BCM: 5, 6, 13, 19 - canais 1 a 4)
RELAY_PINS = {"1": 5, "2": 6, "3": 13, "4": 19}

# Entradas digitais: 1–4 = Reed Switch NA (porta); 5–8 = Botões. Um fio no GPIO, outro no GND.
SENSOR_PINS = {
    "1": 17, "2": 27, "3": 22, "4": 23,
    "5": 24, "6": 25, "7": 26, "8": 4,
}
SENSOR_PINS_PHYSICAL = {
    "1": 11, "2": 13, "3": 15, "4": 16,
    "5": 18, "6": 22, "7": 37, "8": 7,
}


class _MockSensor:
    """Objeto mock quando GPIO dos sensores não está disponível (ex.: permissão)."""
    value = False


class _MockRelay:
    """Objeto mock quando GPIO não está disponível (ex.: rodando no Windows/sem GPIO)."""
    value = False

    def on(self):
        self.value = True

    def off(self):
        self.value = False

    def toggle(self):
        self.value = not self.value


try:
    reles = {
        id: OutputDevice(pin, active_high=False, initial_value=False)
        for id, pin in RELAY_PINS.items()
    }
except Exception as e:
    logging.warning("Relés GPIO não inicializados (%s). Use modo mock (sem controle físico real).", e)
    reles = {id: _MockRelay() for id in RELAY_PINS}

try:
    sensores = {
        id: DigitalInputDevice(pin, pull_up=True)
        for id, pin in SENSOR_PINS.items()
    }
except Exception as e:
    logging.warning("Sensores GPIO não inicializados (%s). Use modo mock. Verifique permissões (gpio, root) ou pin factory.", e)
    sensores = {id: _MockSensor() for id in SENSOR_PINS}

# Timers de pulso por relé (id -> threading.Timer) para cancelar se acionar de novo
_pulse_timers: dict[str, threading.Timer] = {}
_timers_lock = threading.Lock()


def _get_pulse_seconds(relay_id: str) -> float:
    """Duração do pulso em segundos para o relé (0 = desativado)."""
    val = (os.environ.get(f"PULSE_RELE_{relay_id}") or "").strip()
    try:
        return max(0.0, float(val))
    except ValueError:
        return 0.0


def _close_relay_after_pulse(relay_id: str) -> None:
    with _timers_lock:
        _pulse_timers.pop(relay_id, None)
    if relay_id in reles:
        reles[relay_id].off()


REED_IDS = ("1", "2", "3", "4")   # sensores magnéticos (porta)
BUTTON_IDS = ("5", "6", "7", "8")  # botões (entrada digital)


def _sensor_status() -> dict[str, str]:
    """Estado: 1–4 = Reed (Aberto/Fechado); 5–8 = Botão (Pressionado/Solto)."""
    out = {}
    for sid, s in sensores.items():
        if sid in REED_IDS:
            out[sid] = "Fechado" if s.value else "Aberto"
        else:
            out[sid] = "Pressionado" if not s.value else "Solto"
    return out


@app.route('/')
@admin_required
def index():
    status = {rid: ("LIGADO" if r.value else "DESLIGADO") for rid, r in reles.items()}
    cfg = read_config()
    pulse_config = {str(i): (cfg.get(f"PULSE_RELE_{i}") or "").strip() for i in range(1, 5)}
    all_status = _sensor_status()
    sensor_status_reed = {k: all_status[k] for k in REED_IDS if k in all_status}
    sensor_status_buttons = {k: all_status[k] for k in BUTTON_IDS if k in all_status}
    return render_template(
        'index.html',
        status=status,
        pulse_config=pulse_config,
        sensor_status_reed=sensor_status_reed,
        sensor_status_buttons=sensor_status_buttons,
        sensor_pins=SENSOR_PINS,
        sensor_pins_physical=SENSOR_PINS_PHYSICAL,
    )


@app.route('/config')
@admin_required
def config_page():
    """Tela dedicada de configuração do Zapy/ZAccess."""
    return render_template('config.html')


@app.route('/face-terminals')
@admin_required
def face_terminals_page():
    """Cadastro dos terminais faciais (Hikvision/Intelbras) que este zapy fala diretamente."""
    return render_template('face_terminals.html')


@app.route('/api/face-terminals', methods=['GET'])
@admin_required
def api_face_terminals_list():
    return jsonify([face_terminals_store.for_display(t) for t in face_terminals_store.list_terminals()])


@app.route('/api/face-terminals', methods=['POST'])
@admin_required
def api_face_terminals_create():
    terminal = face_terminals_store.create_terminal(request.get_json() or {})
    return jsonify(face_terminals_store.for_display(terminal)), 201


@app.route('/api/face-terminals/<terminal_id>', methods=['PUT'])
@admin_required
def api_face_terminals_update(terminal_id):
    terminal = face_terminals_store.update_terminal(terminal_id, request.get_json() or {})
    if terminal is None:
        return jsonify({"success": False, "message": "terminal não encontrado"}), 404
    return jsonify(face_terminals_store.for_display(terminal))


@app.route('/api/face-terminals/<terminal_id>', methods=['DELETE'])
@admin_required
def api_face_terminals_delete(terminal_id):
    if not face_terminals_store.delete_terminal(terminal_id):
        return jsonify({"success": False, "message": "terminal não encontrado"}), 404
    return jsonify({"success": True})


@app.route('/api/face-terminals/<terminal_id>/test', methods=['POST'])
@admin_required
def api_face_terminals_test(terminal_id):
    """Chama um endpoint leve e sem efeito colateral (system/info ou deviceInfo) só pra
    confirmar que dá pra falar com o terminal com essas credenciais."""
    terminal = face_terminals_store.get_terminal(terminal_id)
    if terminal is None:
        return jsonify({"success": False, "message": "terminal não encontrado"}), 404
    try:
        client = create_face_client(
            terminal["vendor"], host=terminal["host"], port=terminal["port"],
            username=terminal["username"], password=terminal["password"],
            https=terminal["https"], verify_tls=terminal["verify_tls"],
            relay_level=terminal.get("relay_level", 0), group_id=terminal.get("group_id") or None,
        )
        return jsonify({"success": True, "info": client.check_health()})
    except FaceProvisioningError as e:
        return jsonify({"success": False, "message": str(e)}), 502
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/vehicle-antennas')
@admin_required
def vehicle_antennas_page():
    """Cadastro das antenas veiculares (Control iD iDUHF) que este zapy fala diretamente
    — mesmo papel de face_terminals_page, mas pra antenas."""
    return render_template('vehicle_antennas.html')


@app.route('/api/vehicle-antennas', methods=['GET'])
@admin_required
def api_vehicle_antennas_list():
    return jsonify([vehicle_antennas_store.for_display(a) for a in vehicle_antennas_store.list_antennas()])


@app.route('/api/vehicle-antennas', methods=['POST'])
@admin_required
def api_vehicle_antennas_create():
    antenna = vehicle_antennas_store.create_antenna(request.get_json() or {})
    return jsonify(vehicle_antennas_store.for_display(antenna)), 201


@app.route('/api/vehicle-antennas/<antenna_id>', methods=['PUT'])
@admin_required
def api_vehicle_antennas_update(antenna_id):
    antenna = vehicle_antennas_store.update_antenna(antenna_id, request.get_json() or {})
    if antenna is None:
        return jsonify({"success": False, "message": "antena não encontrada"}), 404
    return jsonify(vehicle_antennas_store.for_display(antenna))


@app.route('/api/vehicle-antennas/<antenna_id>', methods=['DELETE'])
@admin_required
def api_vehicle_antennas_delete(antenna_id):
    if not vehicle_antennas_store.delete_antenna(antenna_id):
        return jsonify({"success": False, "message": "antena não encontrada"}), 404
    return jsonify({"success": True})


def _uhf_client_from_stored(antenna: dict) -> ControlIdUhfClient:
    return ControlIdUhfClient(
        ControlIdTerminal(
            host=antenna["host"], port=antenna["port"],
            username=antenna["username"], password=antenna["password"],
            https=antenna["https"], verify_tls=antenna["verify_tls"],
            group_id=antenna.get("group_id") or None,
        ),
        gate_output=antenna.get("gate_output", "contact"),
        door_id=antenna.get("door_id", 1),
        secbox_id=antenna.get("secbox_id") or None,
    )


@app.route('/api/vehicle-antennas/<antenna_id>/test', methods=['POST'])
@admin_required
def api_vehicle_antennas_test(antenna_id):
    """Mesmo papel de api_face_terminals_test, pra antena UHF."""
    antenna = vehicle_antennas_store.get_antenna(antenna_id)
    if antenna is None:
        return jsonify({"success": False, "message": "antena não encontrada"}), 404
    try:
        client = _uhf_client_from_stored(antenna)
        return jsonify({"success": True, "info": client.check_health()})
    except FaceProvisioningError as e:
        return jsonify({"success": False, "message": str(e)}), 502
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/cockpit/vehicle-antennas/<antenna_id>/open-gate', methods=['POST'])
@login_required
def api_cockpit_vehicle_antenna_open(antenna_id):
    """Abertura remota da cancela direto na antena (local, não passa pelo ZAccess) — mesmo
    caminho de api_cockpit_face_terminal_open, chamando open_gate()."""
    antenna = vehicle_antennas_store.get_antenna(antenna_id)
    if antenna is None:
        return jsonify({"success": False, "message": "antena não encontrada"}), 404
    if session.get("role") != "admin" and not antenna.get("cockpit_enabled", True):
        return jsonify({"success": False, "message": "antena não liberada pro porteiro"}), 403
    try:
        client = _uhf_client_from_stored(antenna)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    result = client.open_gate()  # nunca lança — {"ok": bool, "reason"?: str}
    if result.get("ok"):
        now = datetime.now(timezone(timedelta(hours=-3)))
        local_store.add_events([{
            "dedupe_key": f"{antenna_id}:cockpit:{now.strftime('%Y%m%dT%H%M%S%f')}",
            "terminal_id": antenna_id, "employee_no": None, "time": now.isoformat(),
            "direction": "unknown", "success": True, "source": "cockpit",
            "picture": None, "device_name": "Liberado pelo Cockpit",
        }])
        return jsonify({"success": True})
    return jsonify({"success": False, "message": result.get("reason") or "falha ao abrir"}), 502


@app.route('/webhooks/controlid/<terminal_id>/new_user_identified.fcgi', methods=['POST'])
def controlid_webhook_user_identified(terminal_id):
    """Callback que o próprio terminal Control iD chama (modo online/push, diferente dos
    outros vendors que são poll — ver plano de integração, seção Control iD). O terminal
    é configurado manualmente (painel web dele) apontando pra
    http://<ip-deste-zapy>:<porta>/webhooks/controlid/<terminal_id>/new_user_identified.fcgi
    — terminal_id é o id do FaceTerminal no ZAccess (o mesmo que aparece em device:config).
    Sem auth aqui de propósito: a doc oficial não documenta nenhum mecanismo de assinatura
    pro lado do "servidor"; mitigado com allowlist de IP (só aceita evento vindo do próprio
    host cadastrado pro terminal_id) em submit_controlid_event.
    Resposta no formato que a doc mostra o terminal esperando de volta."""
    data = request.get_json(silent=True) or {}
    submit_controlid_event(terminal_id, data, remote_addr=request.remote_addr)
    return jsonify({"result": {"event": data.get("event"), "user_id": data.get("user_id")}})


EVENTS_PAGE_SIZE_DEFAULT = 25
EVENTS_PAGE_SIZE_MAX = 100


def _parse_event_filters() -> dict:
    """Filtros comuns a /api/events e /api/events.csv, lidos da querystring."""
    terminal_id = (request.args.get("terminal") or "").strip() or None
    status = (request.args.get("status") or "").strip().lower()
    success = True if status == "success" else (False if status == "fail" else None)
    start_date = (request.args.get("start") or "").strip()
    end_date = (request.args.get("end") or "").strip()
    # Datas vêm só como YYYY-MM-DD do <input type=date>; -03:00 pra bater com o offset
    # que os eventos já usam (ver _BR_TZ em local_store.py).
    start = f"{start_date}T00:00:00-03:00" if start_date else None
    end = f"{end_date}T23:59:59-03:00" if end_date else None
    q = (request.args.get("q") or "").strip() or None
    return {"terminal_id": terminal_id, "success": success, "start": start, "end": end, "q": q}


def _parse_pagination() -> tuple[int, int]:
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    try:
        page_size = int(request.args.get("pageSize") or EVENTS_PAGE_SIZE_DEFAULT)
    except ValueError:
        page_size = EVENTS_PAGE_SIZE_DEFAULT
    return page, max(1, min(page_size, EVENTS_PAGE_SIZE_MAX))


def _resolve_terminal_names() -> dict[str, str]:
    """Nome de cada terminal/antena — prioriza o nome vindo do ZAccess (device:config), cai
    pro cadastro local (face_terminals_store/vehicle_antennas_store), senão mostra o id cru."""
    local_names = {t["id"]: (t.get("name") or t["id"]) for t in face_terminals_store.list_terminals()}
    local_names.update({a["id"]: (a.get("name") or a["id"]) for a in vehicle_antennas_store.list_antennas()})
    ids = set(local_store.list_event_terminal_ids()) | set(local_names.keys())
    return {tid: (local_store.get_terminal_name(tid) or local_names.get(tid) or tid) for tid in ids}


def _event_to_dict(e, terminal_names: dict[str, str]) -> dict:
    return {
        "dedupeKey": e.dedupe_key,
        "terminalId": e.terminal_id,
        "terminalName": terminal_names.get(e.terminal_id, e.terminal_id),
        "employeeNo": e.employee_no,
        "name": e.name,
        "time": e.time,
        "direction": e.direction,
        "directionLabel": EVENT_DIRECTION_LABEL.get(e.direction, "—"),
        "success": e.success,
        "hasPhoto": e.has_picture or (e.name is not None),
    }


@app.route('/eventos')
@login_required
def events_page():
    """Log de eventos de reconhecimento facial (abertura), com a foto capturada
    na hora (ou a do cadastro, se a captura não estiver disponível)."""
    return render_template('events.html')


@app.route('/api/events')
@login_required
def api_events():
    filters = _parse_event_filters()
    page, page_size = _parse_pagination()
    events, total = local_store.query_events(**filters, limit=page_size, offset=(page - 1) * page_size)
    terminal_names = _resolve_terminal_names()
    return jsonify({
        "events": [_event_to_dict(e, terminal_names) for e in events],
        "total": total,
        "page": page,
        "pageSize": page_size,
        "totalPages": max(1, -(-total // page_size)),
    })


@app.route('/api/events/terminals')
@login_required
def api_events_terminals():
    """Terminais que já têm pelo menos um evento — popula o filtro por terminal."""
    names = _resolve_terminal_names()
    ids = local_store.list_event_terminal_ids()
    return jsonify([{"id": tid, "name": names.get(tid, tid)} for tid in sorted(ids, key=lambda i: names.get(i, i))])


@app.route('/api/events.csv')
@login_required
def api_events_csv():
    """Relatório (CSV) dos eventos filtrados — mesmos filtros de /api/events, sem paginar
    (teto de 5000 linhas, alto o bastante pra não truncar um relatório razoável)."""
    filters = _parse_event_filters()
    events, _ = local_store.query_events(**filters, limit=5000, offset=0)
    terminal_names = _resolve_terminal_names()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Data/hora", "Terminal", "Pessoa", "Matrícula", "Direção", "Resultado"])
    for e in events:
        writer.writerow([
            e.time, terminal_names.get(e.terminal_id, e.terminal_id), e.name or "",
            e.employee_no or "", EVENT_DIRECTION_LABEL.get(e.direction, "-"),
            "Liberado" if e.success else "Negado",
        ])
    # BOM (utf-8-sig): Excel abre acentuação certo sem isso perguntar encoding.
    return Response(
        buf.getvalue().encode("utf-8-sig"), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=eventos_zapy.csv"},
    )


@app.route('/api/events/<path:dedupe_key>/photo')
@login_required
def api_event_photo(dedupe_key):
    """Foto capturada NO MOMENTO do evento (o terminal tira uma foto a cada reconhecimento
    bem-sucedido) — cai pra foto do cadastro da pessoa só se a captura não tiver vindo."""
    jpeg = local_store.get_event_picture(dedupe_key)
    if jpeg is None:
        event = local_store.get_event(dedupe_key)
        if event and event.employee_no:
            jpeg = local_store.get_photo(event.terminal_id, event.employee_no)
    if jpeg is None:
        return '', 404
    return Response(jpeg, mimetype='image/jpeg')


def _cockpit_allowed_relay_ids() -> set[str]:
    """IDs de relé liberados pro porteiro — todos por padrão (COCKPIT_RELAYS vazio/ausente),
    até o admin restringir em /config. "none" é o marcador explícito de "nenhum liberado"
    (senão fica ambíguo com "vazio = todos" quando o admin desmarca tudo)."""
    raw = (read_config().get("COCKPIT_RELAYS") or "").strip()
    if not raw:
        return set(RELAY_PINS.keys())
    if raw == "none":
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()} & set(RELAY_PINS.keys())


@app.route('/cockpit')
@login_required
def cockpit_page():
    """Painel operacional pro porteiro: abrir relés/terminais e ver eventos — sem os
    detalhes de baixo nível (GPIO cru, sensores, config) do painel admin. Admin vê tudo
    (mesmo o que está desmarcado pro porteiro); porteiro só vê o que foi liberado."""
    is_admin = session.get("role") == "admin"
    allowed_relays = set(RELAY_PINS.keys()) if is_admin else _cockpit_allowed_relay_ids()
    relays = [{"id": rid, "pulseSeconds": _get_pulse_seconds(rid)} for rid in RELAY_PINS if rid in allowed_relays]
    terminals = [
        face_terminals_store.for_display(t) for t in face_terminals_store.list_terminals()
        if is_admin or t.get("cockpit_enabled", True)
    ]
    antennas = [
        vehicle_antennas_store.for_display(a) for a in vehicle_antennas_store.list_antennas()
        if is_admin or a.get("cockpit_enabled", True)
    ]
    return render_template('cockpit.html', relays=relays, terminals=terminals, antennas=antennas)


@app.route('/api/cockpit/relays/<id>/open', methods=['POST'])
@login_required
def api_cockpit_relay_open(id):
    """Sempre ABRE (nunca fecha um relé já aberto, ao contrário de /toggle) — é o botão
    do porteiro, não faz sentido ele "fechar a porta" sem querer clicando de novo."""
    if id not in reles:
        return jsonify({"success": False, "message": "relé não encontrado"}), 404
    if session.get("role") != "admin" and id not in _cockpit_allowed_relay_ids():
        return jsonify({"success": False, "message": "relé não liberado pro porteiro"}), 403
    sec = _get_pulse_seconds(id)
    with _timers_lock:
        old = _pulse_timers.pop(id, None)
        if old:
            old.cancel()
    reles[id].on()
    if sec > 0:
        t = threading.Timer(sec, _close_relay_after_pulse, args=[id])
        t.daemon = True
        with _timers_lock:
            _pulse_timers[id] = t
        t.start()
    return jsonify({"success": True, "pulseSeconds": sec})


@app.route('/api/cockpit/face-terminals/<terminal_id>/open', methods=['POST'])
@login_required
def api_cockpit_face_terminal_open(terminal_id):
    """Abertura remota direto no terminal facial (local, não passa pelo ZAccess) —
    mesmo caminho de /api/face-terminals/<id>/test, mas chamando open_door()."""
    terminal = face_terminals_store.get_terminal(terminal_id)
    if terminal is None:
        return jsonify({"success": False, "message": "terminal não encontrado"}), 404
    if session.get("role") != "admin" and not terminal.get("cockpit_enabled", True):
        return jsonify({"success": False, "message": "terminal não liberado pro porteiro"}), 403
    try:
        client = create_face_client(
            terminal["vendor"], host=terminal["host"], port=terminal["port"],
            username=terminal["username"], password=terminal["password"],
            https=terminal["https"], verify_tls=terminal["verify_tls"],
            relay_level=terminal.get("relay_level", 0), group_id=terminal.get("group_id") or None,
        )
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    result = client.open_door()  # nunca lança — {"ok": bool, "reason"?: str}
    if result.get("ok"):
        # Registra no log de Eventos igual um reconhecimento facial — sem isso a abertura
        # pelo cockpit fica invisível no painel (não passa por reconhecimento, não gera
        # AcsEvent/doorlog nenhum pra poller pegar). device_name fixo em vez de tentar
        # inferir da resposta do device: aqui sempre foi o porteiro clicando, não tem
        # ambiguidade a resolver como no doorlog do XPE.
        now = datetime.now(timezone(timedelta(hours=-3)))
        capture_snapshot = getattr(client, "capture_snapshot", None)
        local_store.add_events([{
            "dedupe_key": f"{terminal_id}:cockpit:{now.strftime('%Y%m%dT%H%M%S%f')}",
            "terminal_id": terminal_id, "employee_no": None, "time": now.isoformat(),
            "direction": "unknown", "success": True, "source": "cockpit",
            "picture": capture_snapshot() if capture_snapshot else None,
            "device_name": "Liberado pelo Cockpit",
        }])
    return jsonify({"success": bool(result.get("ok")), "reason": result.get("reason")}), (200 if result.get("ok") else 502)


@app.route('/usuarios')
@admin_required
def users_page():
    """Contas de acesso ao painel (porteiro etc.) além do admin de bootstrap — ver
    users_store.py."""
    return render_template('users.html')


@app.route('/api/users', methods=['GET'])
@admin_required
def api_users_list():
    return jsonify([users_store.for_display(u) for u in users_store.list_users()])


@app.route('/api/users', methods=['POST'])
@admin_required
def api_users_create():
    try:
        user = users_store.create_user(request.get_json() or {})
        return jsonify(users_store.for_display(user)), 201
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400


@app.route('/api/users/<user_id>', methods=['PUT'])
@admin_required
def api_users_update(user_id):
    user = users_store.update_user(user_id, request.get_json() or {})
    if user is None:
        return jsonify({"success": False, "message": "usuário não encontrado"}), 404
    return jsonify(users_store.for_display(user))


@app.route('/api/users/<user_id>', methods=['DELETE'])
@admin_required
def api_users_delete(user_id):
    if session.get("username") and users_store.get_user(user_id) and \
            users_store.get_user(user_id)["username"] == session["username"]:
        return jsonify({"success": False, "message": "não dá pra remover o próprio usuário logado"}), 400
    if not users_store.delete_user(user_id):
        return jsonify({"success": False, "message": "usuário não encontrado"}), 404
    return jsonify({"success": True})


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = (request.form.get('username') or "").strip()
        password = (request.form.get('password') or "").strip()
        # Admin de bootstrap (sempre existe, mesmo com users.json vazio/ausente — não dá
        # pra ficar sem acesso nenhum). Contas extras (porteiro, outros admins) via /usuarios.
        bootstrap_user = os.environ.get("ZAPY_ADMIN_USER", "admin")
        bootstrap_pass = os.environ.get("ZAPY_ADMIN_PASSWORD", "zroot")
        stored = users_store.find_by_username(username)

        role = None
        if username == bootstrap_user and password == bootstrap_pass:
            role = "admin"
        elif stored and stored.get("active", True) and users_store.verify_password(stored, password):
            role = stored["role"]

        if role:
            session["logged_in"] = True
            session["role"] = role
            session["username"] = username
            default_next = url_for("index") if role == "admin" else url_for("cockpit_page")
            return redirect(request.args.get("next") or default_next)
        error = "Usuário ou senha inválidos."
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/health')
@admin_required
def health_page():
    return render_template('health.html')


@app.route('/api/sensors')
@admin_required
def api_sensors():
    """Retorna o estado atual dos 8 entradas (4 reed + 4 botões) para polling."""
    return jsonify(_sensor_status())


@app.route('/toggle/<id>', methods=['POST'])
@admin_required
def toggle(id):
    if id not in reles:
        return redirect(url_for('index'))
    sec = _get_pulse_seconds(id)
    if sec > 0:
        with _timers_lock:
            old = _pulse_timers.pop(id, None)
            if old:
                old.cancel()
        reles[id].on()
        t = threading.Timer(sec, _close_relay_after_pulse, args=[id])
        t.daemon = True
        with _timers_lock:
            _pulse_timers[id] = t
        t.start()
    else:
        reles[id].toggle()
    return redirect(url_for('index'))


# --- Configuração ZAccess (variáveis de ambiente) ---
@app.route('/api/config', methods=['GET'])
@admin_required
def api_config_get():
    """Retorna as variáveis de ambiente do ZAccess para o frontend (token mascarado)."""
    return jsonify(get_config_for_display())


@app.route('/api/config', methods=['POST'])
@admin_required
def api_config_post():
    """Atualiza variáveis de ambiente no .env e recarrega load_dotenv (não reinicia o processo)."""
    try:
        data = request.get_json() or {}
        write_config(data)
        load_dotenv(override=True)
        return jsonify({"success": True, "message": "Configuração salva. Reinicie o serviço para aplicar a conexão com o ZAccess."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/restart', methods=['POST'])
@admin_required
def api_restart():
    """
    Agenda reinício do serviço systemd (zapy). Responde antes de executar.
    Requer permissão: sudo systemctl restart zapy (ex.: sudoers NOPASSWD).
    """
    service_name = os.environ.get("ZAPY_SERVICE_NAME", "zapy")

    def do_restart():
        time.sleep(1.5)
        try:
            subprocess.run(
                ["sudo", "systemctl", "restart", service_name],
                capture_output=True,
                timeout=10,
            )
        except Exception as e:
            logging.warning("Falha ao reiniciar serviço: %s", e)

    threading.Thread(target=do_restart, daemon=True).start()
    return jsonify({
        "success": True,
        "message": "Reinício do serviço em andamento. A página pode desconectar em instantes.",
    }), 202


if __name__ == '__main__':
    # Conecta ao ZAccess; envia relés e estado dos sensores/botões (inputs)
    start_zaccess_client_in_background(reles, sensores=sensores, sensor_pins=SENSOR_PINS)
    # Painel local (porta do .env ou 3080)
    port_str = (os.environ.get("PORT") or "3080").strip()
    port = int(port_str) if port_str else 3080
    app.run(host='0.0.0.0', port=port)
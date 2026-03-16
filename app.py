import os
import logging
import subprocess
import threading
import time

from dotenv import load_dotenv
from flask import Flask, render_template, redirect, url_for, request, jsonify, session
from functools import wraps

load_dotenv()
from gpiozero import OutputDevice, DigitalInputDevice

from zaccess_client import start_zaccess_client_in_background
from config_env import read_config, get_config_for_display, write_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

app = Flask(__name__, template_folder='painel_rele/templates')
app.secret_key = os.environ.get("ZAPY_SECRET_KEY", "dev-zapy-secret")


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)

    return wrapper

# Configuração dos Relés (Pinos BCM: 5, 6, 13, 19 - canais 1 a 4)
reles = {
    "1": OutputDevice(5,  active_high=False, initial_value=False),
    "2": OutputDevice(6,  active_high=False, initial_value=False),
    "3": OutputDevice(13, active_high=False, initial_value=False),
    "4": OutputDevice(19, active_high=False, initial_value=False),
}

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
@login_required
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
@login_required
def config_page():
    """Tela dedicada de configuração do Zapy/ZAccess."""
    return render_template('config.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = (request.form.get('username') or "").strip()
        password = (request.form.get('password') or "").strip()
        if username == "admin" and password == "zroot":
            session["logged_in"] = True
            next_url = request.args.get("next") or url_for("index")
            return redirect(next_url)
        error = "Usuário ou senha inválidos."
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/health')
@login_required
def health_page():
    return render_template('health.html')


@app.route('/api/sensors')
def api_sensors():
    """Retorna o estado atual dos 8 entradas (4 reed + 4 botões) para polling."""
    return jsonify(_sensor_status())


@app.route('/toggle/<id>')
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
def api_config_get():
    """Retorna as variáveis de ambiente do ZAccess para o frontend (token mascarado)."""
    return jsonify(get_config_for_display())


@app.route('/api/config', methods=['POST'])
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
import os
import logging
import subprocess
import threading
import time

from dotenv import load_dotenv
from flask import Flask, render_template, redirect, url_for, request, jsonify

load_dotenv()
from gpiozero import OutputDevice

from zaccess_client import start_zaccess_client_in_background
from config_env import read_config, get_config_for_display, write_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

app = Flask(__name__, template_folder='painel_rele/templates')

# Configuração dos Relés (Pinos BCM: 5, 6, 13, 19 - canais 1 a 4)
reles = {
    "1": OutputDevice(5,  active_high=False, initial_value=False),
    "2": OutputDevice(6,  active_high=False, initial_value=False),
    "3": OutputDevice(13, active_high=False, initial_value=False),
    "4": OutputDevice(19, active_high=False, initial_value=False),
}

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


@app.route('/')
def index():
    status = {rid: ("LIGADO" if r.value else "DESLIGADO") for rid, r in reles.items()}
    cfg = read_config()
    pulse_config = {str(i): (cfg.get(f"PULSE_RELE_{i}") or "").strip() for i in range(1, 5)}
    return render_template('index.html', status=status, pulse_config=pulse_config)


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
    # Conecta ao ZAccess se ZACCESS_SERVER_URL e ZACCESS_DEVICE_SERIAL estiverem definidos
    start_zaccess_client_in_background(reles)
    # Painel local (porta do .env ou 3080)
    port_str = (os.environ.get("PORT") or "3080").strip()
    port = int(port_str) if port_str else 3080
    app.run(host='0.0.0.0', port=port)
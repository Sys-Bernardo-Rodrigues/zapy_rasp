from flask import Flask, render_template, redirect, url_for
from gpiozero import OutputDevice

app = Flask(__name__, template_folder='painel_rele/templates')

# Configuração dos Relés (Pinos BCM baseados na sua ligação anterior)
reles = {
    "1": OutputDevice(5,  active_high=False, initial_value=False),
    "2": OutputDevice(6,  active_high=False, initial_value=False),
    "3": OutputDevice(13, active_high=False, initial_value=False),
    "4": OutputDevice(19, active_high=False, initial_value=False)
}

@app.route('/')
def index():
    # Passa o estado atual de cada relé para o HTML
    status = {id: ("LIGADO" if r.value else "DESLIGADO") for id, r in reles.items()}
    return render_template('index.html', status=status)

@app.route('/toggle/<id>')
def toggle(id):
    if id in reles:
        reles[id].toggle()
    return redirect(url_for('index'))

if __name__ == '__main__':
    # Rodando na porta 3080 em todos os IPs da rede local
    app.run(host='0.0.0.0', port=3080)
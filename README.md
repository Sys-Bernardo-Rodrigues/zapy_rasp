# Zapy – Painel de relés + cliente ZAccess

Painel web local (Flask) para controle de 4 relés via GPIO no Raspberry Pi, com opção de ser **controlado pelo sistema [ZAccess](https://github.com/Sys-Bernardo-Rodrigues/Projeto-ZAccess)** via Socket.IO.

## Requisitos

- Python 3.10+
- Raspberry Pi (GPIO: pinos BCM 5, 6, 13, 19 para os canais 1–4)

## Instalação

### Instalação completa (recomendado no Raspberry Pi)

Instala ambiente virtual, dependências e **serviço systemd** (inicia com o sistema):

```bash
./install.sh
```

Será pedido **sudo** para instalar e iniciar o serviço. O Zapy passa a rodar como serviço `zapy`:

- **Status:** `sudo systemctl status zapy`
- **Logs:** `journalctl -u zapy -f`
- **Reiniciar:** `sudo systemctl restart zapy`

O painel tem um botão **"Reiniciar serviço"** (em Configurações). Para funcionar sem pedir senha, permita o usuário que roda o Zapy executar apenas esse comando. Crie um arquivo em `/etc/sudoers.d/zapy` (por exemplo) com:

```
zaccess ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart zapy
```

(Substitua `zaccess` pelo usuário do sistema que executa o serviço.)

Para instalar só o ambiente (sem serviço, sem sudo):

```bash
./install.sh --no-service
```

### Desinstalação

Remove o serviço systemd (para e desabilita):

```bash
./uninstall.sh
```

Para remover também o ambiente virtual (`.venv`):

```bash
./uninstall.sh --purge
```

### Instalação manual (sem scripts)

```bash
python3 -m venv .venv
source .venv/bin/activate   # Linux/macOS
pip install -r requirements.txt
```

## Uso só do painel local

```bash
python app.py
```

Acesse **http://\<IP-do-Pi\>:3080** para ligar/desligar os relés.

## Integração com o ZAccess

Para este dispositivo ser **controlado pelo ZAccess** (painel admin ou app):

1. **Cadastre o dispositivo no ZAccess**
   - No painel admin do ZAccess, crie um **Local** (se ainda não existir).
   - Cadastre um **Dispositivo** nesse local com:
     - **Nome**: ex. "Portaria Zapy"
     - **Número de série**: um identificador único (ex. `ZAPY-001`). Este valor será usado na variável `ZACCESS_DEVICE_SERIAL`.
     - (Opcional) Defina um **Token** no dispositivo e use o mesmo em `ZACCESS_DEVICE_TOKEN`.

2. **Cadastre os 4 relés no dispositivo**
   - Crie 4 relés associados a esse dispositivo com:
     - **Canal**: 1, 2, 3 e 4 (obrigatório).
     - **Pino GPIO**: 5, 6, 13 e 19 (conforme o hardware).
   - Os nomes e tipos (porta, luz, etc.) são livres.

3. **Configure as variáveis de ambiente**
   - Copie o exemplo e ajuste:
   ```bash
   cp .env.example .env
   ```
   - Edite `.env` e defina:
   - `ZACCESS_SERVER_URL` = URL do servidor ZAccess (ex.: `http://192.168.1.100:3000`)
   - `ZACCESS_DEVICE_SERIAL` = mesmo número de série cadastrado no passo 1
   - `ZACCESS_DEVICE_TOKEN` = só se tiver definido token no dispositivo

4. **Carregue o `.env` e inicie o app**
   ```bash
   export $(grep -v '^#' .env | xargs)
   python app.py
   ```
   Ou use um gerenciador de ambiente (systemd, etc.) que carregue o `.env`.

O cliente Socket.IO conecta ao namespace `/devices` do ZAccess, envia **heartbeat** e obedece ao comando **relay:toggle**. O painel local (porta 3080) continua funcionando em paralelo.

## Variáveis de ambiente

| Variável | Descrição |
|----------|-----------|
| `ZACCESS_SERVER_URL` | URL base do servidor ZAccess (ex.: `http://IP:3000`) |
| `ZACCESS_DEVICE_SERIAL` | Número de série do dispositivo no ZAccess |
| `ZACCESS_DEVICE_TOKEN` | Token do dispositivo (opcional) |
| `PORT` | Porta do painel web local (padrão: 3080) |

## Hardware – pinos (BCM / físico no header 40 v2/v3)

**Relés (saídas)**  
| Canal | BCM | Pino físico |
|-------|-----|-------------|
| 1     | 5   | 29          |
| 2     | 6   | 31          |
| 3     | 13  | 33          |
| 4     | 19  | 35          |

**Sensores magnéticos Reed Switch NA (1–4)**  
Um terminal do reed no GPIO, outro no **GND**. Ímã perto = **Fechado**, ímã longe = **Aberto**.

| Sensor | BCM | Pino físico |
|--------|-----|-------------|
| 1      | 17  | 11          |
| 2      | 27  | 13          |
| 3      | 22  | 15          |
| 4      | 23  | 16          |

**Botões (5–8)**  
Um terminal do botão no GPIO, outro no **GND**. Pressionado = **Pressionado**, solto = **Solto**.

| Botão | BCM | Pino físico |
|-------|-----|-------------|
| 5     | 24  | 18          |
| 6     | 25  | 22          |
| 7     | 26  | 37          |
| 8     | 4   | 7           |

Se o serviço falhar ao iniciar por causa dos sensores (erro em `/sys/class/gpio` ou `Invalid argument`), o Zapy passa a usar **modo mock** (sensores sempre “Fechado”) e o restante funciona. Para os sensores reais funcionarem, o usuário que roda o serviço precisa ter acesso ao GPIO: `sudo usermod -aG gpio zaccess` (troque `zaccess` pelo usuário) e depois fazer logout/login ou reiniciar. Para usar o pin factory `lgpio` (recomendado no Raspberry Pi): instale no sistema `sudo apt install liblgpio-dev` e no venv do Zapy `.venv/bin/pip install lgpio` (não use o `pip` do sistema).

## Estrutura

- `app.py` – Flask: painel web e inicialização do cliente ZAccess
- `zaccess_client.py` – Cliente Socket.IO (namespace `/devices`): relay:toggle, heartbeat, relay:state-update
- `painel_rele/templates/index.html` – Interface do painel local

## Licença

Uso interno / conforme definido pelo projeto.

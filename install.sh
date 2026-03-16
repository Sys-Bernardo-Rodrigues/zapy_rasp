#!/usr/bin/env bash
# Zapy – Instalação: ambiente virtual, dependências e serviço systemd
# Uso: ./install.sh [--no-service]
#   --no-service  só instala venv e dependências (não precisa de sudo)

set -e

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="zapy"
NO_SERVICE=false

for arg in "$@"; do
  case "$arg" in
    --no-service) NO_SERVICE=true ;;
    -h|--help)
      echo "Uso: $0 [--no-service]"
      echo "  Instala venv, dependências e (por padrão) serviço systemd."
      echo "  --no-service  apenas venv + pip install (sem sudo)"
      exit 0
      ;;
  esac
done

echo "[zapy] Diretório: $INSTALL_DIR"
cd "$INSTALL_DIR"

if [[ "$(id -u)" -ne 0 ]] && ! $NO_SERVICE; then
  echo "[zapy] Será pedido sudo ao final para instalar o serviço."
fi

# --- .env (copiar exemplo se não existir) ---
if [[ ! -f ".env" ]] && [[ -f ".env.example" ]]; then
  cp .env.example .env
  echo "[zapy] Arquivo .env criado a partir de .env.example. Ajuste se necessário."
fi

# --- Ambiente virtual e dependências (só como usuário normal) ---
if [[ "$(id -u)" -ne 0 ]]; then
  if [[ ! -d ".venv" ]]; then
    echo "[zapy] Criando ambiente virtual..."
    if ! python3 -m venv .venv 2>/dev/null; then
      echo "[zapy] AVISO: python3 -m venv falhou. Tente: sudo apt install python3-full python3-venv"
      exit 1
    fi
  fi
  echo "[zapy] Instalando dependências Python..."
  "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
  "$INSTALL_DIR/.venv/bin/pip" install -q -r requirements.txt
  echo "[zapy] Dependências instaladas."
fi

if $NO_SERVICE; then
  echo "[zapy] Concluído (sem serviço). Para rodar: $INSTALL_DIR/.venv/bin/python app.py"
  exit 0
fi

# --- Serviço systemd (requer root) ---
if [[ "$(id -u)" -ne 0 ]]; then
  echo "[zapy] Instalando serviço systemd (sudo)..."
  exec sudo "$0" "$@"
fi

RUN_AS_USER="${SUDO_USER:-root}"
if [[ "$RUN_AS_USER" == "root" ]]; then
  RUN_AS_USER="$(stat -c '%U' "$INSTALL_DIR" 2>/dev/null || echo 'root')"
fi

# --- Pacotes opcionais para GPIO (Raspberry Pi / sensores) ---
if command -v apt-get &>/dev/null; then
  if ! dpkg -l liblgpio-dev &>/dev/null 2>&1; then
    echo "[zapy] Instalando liblgpio-dev (para sensores GPIO)..."
    apt-get update -qq && apt-get install -y liblgpio-dev &>/dev/null || true
  fi
  if getent group gpio &>/dev/null && [[ "$RUN_AS_USER" != "root" ]]; then
    if ! groups "$RUN_AS_USER" 2>/dev/null | grep -q '\bgpio\b'; then
      echo "[zapy] Adicionando usuário $RUN_AS_USER ao grupo gpio (acesso aos sensores)."
      usermod -aG gpio "$RUN_AS_USER" 2>/dev/null || true
    fi
  fi
fi

UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
echo "[zapy] Instalando serviço: $UNIT_FILE (User=$RUN_AS_USER)"

cat > "$UNIT_FILE" << EOF
[Unit]
Description=Zapy - Painel de relés, sensores e cliente ZAccess
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_AS_USER
WorkingDirectory=$INSTALL_DIR
Environment=PATH=$INSTALL_DIR/.venv/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=-$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/.venv/bin/python app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl start "$SERVICE_NAME"

echo ""
echo "[zapy] Instalação concluída."
echo "  Painel:  http://$(hostname -I 2>/dev/null | awk '{print $1}'):3080"
echo "  status:  sudo systemctl status $SERVICE_NAME"
echo "  logs:    journalctl -u $SERVICE_NAME -f"
echo "  parar:   sudo systemctl stop $SERVICE_NAME"
exit 0

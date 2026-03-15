#!/usr/bin/env bash
# Zapy – Instalação: ambiente virtual, dependências e serviço systemd
# Uso: ./install.sh [--no-service]
#   --no-service  só instala venv e dependências, não instala/inicia o serviço (não precisa de sudo)

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

echo "[zapy] Diretório de instalação: $INSTALL_DIR"
cd "$INSTALL_DIR"

# Se não for root e quiser serviço, reexecutar com sudo (só para instalar o serviço)
if [[ "$(id -u)" -ne 0 ]] && ! $NO_SERVICE; then
  echo "[zapy] Ambiente virtual e dependências serão instalados; em seguida será pedido sudo para o serviço."
fi

# --- Ambiente virtual e dependências (só como usuário normal, para não criar .venv como root) ---
if [[ "$(id -u)" -ne 0 ]]; then
  if [[ ! -d ".venv" ]]; then
    echo "[zapy] Criando ambiente virtual..."
    python3 -m venv .venv
  fi
  echo "[zapy] Instalando dependências..."
  "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
  "$INSTALL_DIR/.venv/bin/pip" install -q -r requirements.txt
  echo "[zapy] Dependências instaladas."
fi

if $NO_SERVICE; then
  echo "[zapy] Instalação concluída (serviço não instalado). Para rodar: $INSTALL_DIR/.venv/bin/python app.py"
  exit 0
fi

# --- Serviço systemd (requer root) ---
if [[ "$(id -u)" -ne 0 ]]; then
  echo "[zapy] Instalando serviço systemd (sudo)..."
  exec sudo "$0" "$@"
fi

# Dono do diretório do projeto (rodar o serviço como esse usuário)
RUN_AS_USER="${SUDO_USER:-root}"
if [[ "$RUN_AS_USER" == "root" ]]; then
  RUN_AS_USER="$(stat -c '%U' "$INSTALL_DIR" 2>/dev/null || echo 'root')"
fi

UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
echo "[zapy] Instalando serviço systemd: $UNIT_FILE (User=$RUN_AS_USER)"

cat > "$UNIT_FILE" << EOF
[Unit]
Description=Zapy - Painel de relés e cliente ZAccess
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
echo "[zapy] Serviço instalado e iniciado: $SERVICE_NAME"
echo "  status:  sudo systemctl status $SERVICE_NAME"
echo "  logs:    journalctl -u $SERVICE_NAME -f"
echo "  parar:  sudo systemctl stop $SERVICE_NAME"
echo "[zapy] Instalação concluída."
exit 0

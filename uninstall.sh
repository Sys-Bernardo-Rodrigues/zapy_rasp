#!/usr/bin/env bash
# Zapy – Desinstalação: para e remove o serviço systemd; opcionalmente remove .venv
# Uso: ./uninstall.sh [--purge]
#   --purge  remove também o ambiente virtual (.venv)

set -e

SERVICE_NAME="zapy"
PURGE=false

for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=true ;;
    -h|--help)
      echo "Uso: $0 [--purge]"
      echo "  Para e remove o serviço systemd."
      echo "  --purge  remove também o diretório .venv"
      exit 0
      ;;
  esac
done

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[zapy] Removendo serviço (sudo)..."
  exec sudo "$0" "$@"
fi

# --- Parar e desabilitar serviço ---
if systemctl is-enabled "$SERVICE_NAME" &>/dev/null; then
  echo "[zapy] Parando e desabilitando $SERVICE_NAME..."
  systemctl stop "$SERVICE_NAME" 2>/dev/null || true
  systemctl disable "$SERVICE_NAME"
fi

UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
if [[ -f "$UNIT_FILE" ]]; then
  rm -f "$UNIT_FILE"
  systemctl daemon-reload
  echo "[zapy] Serviço removido: $UNIT_FILE"
else
  echo "[zapy] Serviço não encontrado: $UNIT_FILE"
fi

# --- Purge: remover .venv ---
if $PURGE && [[ -d "$INSTALL_DIR/.venv" ]]; then
  echo "[zapy] Removendo .venv..."
  rm -rf "$INSTALL_DIR/.venv"
  echo "[zapy] .venv removido."
fi

echo "[zapy] Desinstalação concluída."
exit 0

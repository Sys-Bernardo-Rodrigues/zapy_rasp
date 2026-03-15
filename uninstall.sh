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

# --- Remover serviço (requer sudo) ---
if [[ "$(id -u)" -ne 0 ]]; then
  echo "[zapy] Para remover o serviço é necessário sudo. Executando: sudo $0 $*"
  exec sudo "$0" "$@"
fi

if systemctl is-enabled "$SERVICE_NAME" &>/dev/null; then
  echo "[zapy] Parando e desabilitando serviço $SERVICE_NAME..."
  systemctl stop "$SERVICE_NAME" 2>/dev/null || true
  systemctl disable "$SERVICE_NAME"
fi

UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
if [[ -f "$UNIT_FILE" ]]; then
  rm -f "$UNIT_FILE"
  systemctl daemon-reload
  echo "[zapy] Serviço removido: $UNIT_FILE"
else
  echo "[zapy] Arquivo de serviço não encontrado: $UNIT_FILE"
fi

# --- Purge: remover .venv ---
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if $PURGE && [[ -d "$INSTALL_DIR/.venv" ]]; then
  echo "[zapy] Removendo ambiente virtual (.venv)..."
  rm -rf "$INSTALL_DIR/.venv"
  echo "[zapy] .venv removido."
fi

echo "[zapy] Desinstalação concluída."
exit 0

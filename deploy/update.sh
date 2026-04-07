#!/bin/bash
# =============================================================================
# deploy/update.sh — Actualiza el bot con el código más reciente de GitHub
#
# Uso (en el servidor):
#   bash ~/bot/deploy/update.sh
#
# Lo que hace:
#   1. git pull origin main
#   2. pip install -r requirements.txt (instala nuevas dependencias si las hay)
#   3. Reinicia el servicio systemd
#   4. Muestra el estado del servicio
# =============================================================================

set -euo pipefail

APP_DIR="$HOME/bot"
VENV_DIR="$APP_DIR/venv"
SERVICE_NAME="polymarket-bot"

echo "Actualizando bot desde GitHub..."
cd "$APP_DIR"

# Detener temporalmente para un update limpio
sudo systemctl stop "$SERVICE_NAME" || true

# Actualizar código
git pull origin main

# Actualizar dependencias si requirements.txt cambió
source "$VENV_DIR/bin/activate"
pip install -r requirements.txt -q

# Reiniciar servicio
sudo systemctl start "$SERVICE_NAME"
sleep 2
sudo systemctl status "$SERVICE_NAME" --no-pager -l

echo ""
echo "Update completado. Logs en tiempo real:"
echo "  sudo journalctl -u $SERVICE_NAME -f"

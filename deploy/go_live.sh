#!/usr/bin/env bash
# deploy/go_live.sh — Transición de paper trading a producción real.
#
# REQUISITOS PREVIOS:
#   1. Cuenta Polymarket activa con USDC depositado en Polygon
#   2. Las credenciales en /home/ubuntu/bot/.env son correctas (L1 + L2)
#   3. Bot estable en paper trading (7+ días sin crash)
#
# USO:
#   bash deploy/go_live.sh
#
# El script NO hace nada destructivo sin confirmación explícita.

set -euo pipefail

SSH_KEY="${SSH_KEY_PATH:-$HOME/.ssh/ssh-key-2026-04-12.key}"
HOST="ubuntu@129.213.115.16"
ENV_FILE="/home/ubuntu/bot/.env"

# ── Parámetros de riesgo para producción (conservadores) ──────────────────
BANKROLL_USD=1000
KELLY_FRACTION=0.15
MAX_BANKROLL_RISK_PCT=0.02
MAX_SESSION_LOSS_PCT=0.03
MAX_TOTAL_EXPOSURE_PCT=0.20

ssh_cmd() {
    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -i "$SSH_KEY" "$HOST" "$@"
}

echo "========================================"
echo "  Polymarket Bot — Go Live"
echo "========================================"
echo ""

# ── 1. Verificar conectividad ──────────────────────────────────────────────
echo "[1/7] Verificando conectividad con el servidor..."
ssh_cmd "echo OK" > /dev/null || { echo "❌ No se puede conectar al servidor. Abortando."; exit 1; }
echo "    ✅ Conectado a $HOST"

# ── 2. Estado actual del bot ───────────────────────────────────────────────
echo ""
echo "[2/7] Estado actual del bot:"
ssh_cmd "sudo systemctl status polymarket-bot --no-pager -l | head -6"

# ── 3. Mostrar configuración actual en servidor ────────────────────────────
echo ""
echo "[3/7] Configuración actual en el servidor:"
ssh_cmd "grep -E 'SIMULATION_MODE|BANKROLL_USD|MAX_SESSION_LOSS_PCT|KELLY_FRACTION|MAX_BANKROLL_RISK_PCT|MAX_TOTAL_EXPOSURE_PCT' $ENV_FILE"

# ── 4. Mostrar los parámetros que se van a aplicar ────────────────────────
echo ""
echo "[4/7] Parámetros que este script aplicará:"
echo "    BANKROLL_USD           = $BANKROLL_USD  (capital real a arriesgar)"
echo "    KELLY_FRACTION         = $KELLY_FRACTION  (15% del Kelly óptimo)"
echo "    MAX_BANKROLL_RISK_PCT  = $MAX_BANKROLL_RISK_PCT  (máx \$20 por orden)"
echo "    MAX_SESSION_LOSS_PCT   = $MAX_SESSION_LOSS_PCT  (kill switch a \$30 pérdida)"
echo "    MAX_TOTAL_EXPOSURE_PCT = $MAX_TOTAL_EXPOSURE_PCT  (máx \$200 en órdenes vivas)"
echo "    SIMULATION_MODE        = false"
echo ""
echo "    → Orden típica (spread 2¢, mid \$0.50): ~\$6 USD"
echo "    → Kill switch si pierde más de \$30 en la sesión"

# ── 5. Confirmación explícita ─────────────────────────────────────────────
echo ""
echo "========================================"
echo "  ⚠️  ADVERTENCIA: DINERO REAL"
echo "========================================"
echo "  Confirma que tienes USDC depositado en tu cuenta"
echo "  de Polymarket (red Polygon) antes de continuar."
echo ""
read -r -p "Escribe 'PRODUCCION' para continuar: " CONFIRM
if [[ "$CONFIRM" != "PRODUCCION" ]]; then
    echo "Abortado."
    exit 0
fi

# ── 6. Backup + aplicar todos los parámetros ─────────────────────────────
echo ""
echo "[5/7] Haciendo backup del .env..."
BACKUP_FILE="${ENV_FILE}.backup_$(date -u +%Y%m%d_%H%M%S)"
ssh_cmd "cp $ENV_FILE $BACKUP_FILE"
echo "    Backup: $BACKUP_FILE"

echo ""
echo "[6/7] Aplicando parámetros de producción..."
ssh_cmd "sed -i \
    -e 's/^SIMULATION_MODE=.*/SIMULATION_MODE=false/' \
    -e 's/^BANKROLL_USD=.*/BANKROLL_USD=$BANKROLL_USD/' \
    -e 's/^KELLY_FRACTION=.*/KELLY_FRACTION=$KELLY_FRACTION/' \
    -e 's/^MAX_BANKROLL_RISK_PCT=.*/MAX_BANKROLL_RISK_PCT=$MAX_BANKROLL_RISK_PCT/' \
    -e 's/^MAX_SESSION_LOSS_PCT=.*/MAX_SESSION_LOSS_PCT=$MAX_SESSION_LOSS_PCT/' \
    -e 's/^MAX_TOTAL_EXPOSURE_PCT=.*/MAX_TOTAL_EXPOSURE_PCT=$MAX_TOTAL_EXPOSURE_PCT/' \
    $ENV_FILE"

echo "    Verificando cambios:"
ssh_cmd "grep -E 'SIMULATION_MODE|BANKROLL_USD|MAX_SESSION_LOSS_PCT|KELLY_FRACTION|MAX_BANKROLL_RISK_PCT|MAX_TOTAL_EXPOSURE_PCT' $ENV_FILE"

# ── 7. Reiniciar y verificar ───────────────────────────────────────────────
echo ""
echo "[7/7] Reiniciando el servicio..."
ssh_cmd "sudo systemctl restart polymarket-bot"
sleep 8

echo ""
echo "    Primeros logs (verificar modo real):"
ssh_cmd "sudo journalctl -u polymarket-bot --no-pager -n 25 -o short"

echo ""
echo "========================================"
echo "  ✅ Bot en producción."
echo ""
echo "     Dashboard: http://129.213.115.16:8080"
echo "     Logs live: ssh -i $SSH_KEY $HOST 'sudo journalctl -u polymarket-bot -f'"
echo ""
echo "  Para REVERTIR a paper trading:"
echo "     ssh -i $SSH_KEY $HOST"
echo "     sudo cp ${ENV_FILE}.backup_* /tmp/  # ver backups"
echo "     sudo sed -i 's/SIMULATION_MODE=false/SIMULATION_MODE=true/' $ENV_FILE"
echo "     sudo systemctl restart polymarket-bot"
echo "========================================"

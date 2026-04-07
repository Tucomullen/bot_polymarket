#!/bin/bash
# =============================================================================
# deploy/setup_oracle.sh — Instalación automática en Oracle Cloud Ubuntu 22.04
#
# Uso (como usuario ubuntu en el servidor):
#   bash setup_oracle.sh
#
# Lo que hace:
#   1. Actualiza paquetes del sistema
#   2. Instala Python 3.11, git, pip
#   3. Clona el repositorio desde GitHub
#   4. Crea el entorno virtual e instala dependencias
#   5. Crea los directorios de trabajo (logs/, backtesting/data/)
#   6. Instala el servicio systemd para auto-arranque
#   7. Abre el puerto 8080 en el firewall local (iptables)
#
# Requisitos:
#   - Ubuntu 22.04 LTS
#   - Acceso a internet
#   - Sudo sin password (estándar en Oracle Cloud para el usuario ubuntu)
# =============================================================================

set -euo pipefail

# ---- Configuración ----
REPO_URL="https://github.com/Tucomullen/bot_polymarket.git"
APP_DIR="$HOME/bot"
VENV_DIR="$APP_DIR/venv"
SERVICE_NAME="polymarket-bot"
PYTHON_VERSION="python3.11"
DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"

echo "============================================="
echo "  POLYMARKET BOT — Setup Oracle Cloud"
echo "============================================="
echo "  Repo:    $REPO_URL"
echo "  Dir:     $APP_DIR"
echo "  Port:    $DASHBOARD_PORT"
echo "============================================="
echo ""

# ---- 1. Actualizar sistema ----
echo "[1/7] Actualizando paquetes del sistema..."
sudo apt-get update -qq
sudo apt-get upgrade -y -qq

# ---- 2. Instalar Python 3.11 y dependencias ----
echo "[2/7] Instalando Python 3.11, git, pip..."
sudo apt-get install -y -qq \
    python3.11 \
    python3.11-venv \
    python3.11-dev \
    python3-pip \
    git \
    curl \
    jq

# Verificar versión
python3.11 --version

# ---- 3. Clonar o actualizar el repositorio ----
echo "[3/7] Clonando repositorio..."
if [ -d "$APP_DIR/.git" ]; then
    echo "  Repositorio ya existe — actualizando..."
    cd "$APP_DIR"
    git pull origin main
else
    git clone "$REPO_URL" "$APP_DIR"
    cd "$APP_DIR"
fi

# ---- 4. Entorno virtual e instalación de dependencias ----
echo "[4/7] Creando entorno virtual e instalando dependencias..."
$PYTHON_VERSION -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "  Dependencias instaladas: OK"

# ---- 5. Crear directorios de trabajo ----
echo "[5/7] Creando directorios de trabajo..."
mkdir -p "$APP_DIR/logs"
mkdir -p "$APP_DIR/backtesting/data"
chmod 755 "$APP_DIR/logs" "$APP_DIR/backtesting/data"

# ---- 6. Configurar .env ----
if [ ! -f "$APP_DIR/.env" ]; then
    echo ""
    echo "  ATENCIÓN: No existe .env en $APP_DIR"
    echo "  Crea el archivo manualmente:"
    echo ""
    echo "    cp $APP_DIR/.env.example $APP_DIR/.env"
    echo "    nano $APP_DIR/.env"
    echo ""
    echo "  Variables mínimas requeridas:"
    echo "    PRIVATE_KEY=tu_clave_privada"
    echo "    SIMULATION_MODE=true"
    echo ""
fi

# ---- 7. Instalar servicio systemd ----
echo "[6/7] Instalando servicio systemd ($SERVICE_NAME)..."

# Reemplazar placeholders en el template del servicio
sed "s|__APP_DIR__|$APP_DIR|g; s|__VENV_DIR__|$VENV_DIR|g; s|__USER__|$(whoami)|g" \
    "$APP_DIR/deploy/bot.service" \
    | sudo tee "/etc/systemd/system/$SERVICE_NAME.service" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
echo "  Servicio instalado y habilitado para auto-arranque"

# ---- 7. Abrir puerto en firewall ----
echo "[7/7] Configurando firewall (iptables)..."
# Regla para iptables (Oracle Cloud usa iptables por defecto)
sudo iptables -C INPUT -p tcp --dport "$DASHBOARD_PORT" -j ACCEPT 2>/dev/null || \
    sudo iptables -I INPUT -p tcp --dport "$DASHBOARD_PORT" -j ACCEPT
# Persistir reglas entre reinicios
if command -v netfilter-persistent &>/dev/null; then
    sudo netfilter-persistent save
else
    sudo apt-get install -y -qq iptables-persistent
    sudo netfilter-persistent save
fi
echo "  Puerto $DASHBOARD_PORT abierto en iptables"

echo ""
echo "============================================="
echo "  Setup completado"
echo "============================================="
echo ""
echo "  PRÓXIMOS PASOS:"
echo ""
echo "  1. Crea el archivo .env:"
echo "     cp $APP_DIR/.env.example $APP_DIR/.env"
echo "     nano $APP_DIR/.env"
echo ""
echo "  2. Arranca el bot:"
echo "     sudo systemctl start $SERVICE_NAME"
echo ""
echo "  3. Comprueba que arrancó:"
echo "     sudo systemctl status $SERVICE_NAME"
echo "     sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "  4. Dashboard (desde tu PC):"
echo "     http://$(hostname -I | awk '{print $1}'):$DASHBOARD_PORT"
echo ""
echo "  RECUERDA: Abre también el puerto $DASHBOARD_PORT en el"
echo "  Security Group de Oracle Cloud (VCN → Security List)."
echo ""

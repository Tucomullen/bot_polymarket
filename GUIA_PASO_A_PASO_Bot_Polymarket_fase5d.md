# Guía Fase 5d — Oracle Cloud + Paper Trading

**Objetivo:** Ejecutar el bot en modo simulación durante 7-14 días en un servidor
gratuito 24/7 para validar estabilidad, reconexiones y coherencia del P&L
simulado antes de pasar a capital real.

---

## Índice

1. [Crear cuenta Oracle Cloud Always Free](#1-crear-cuenta-oracle-cloud-always-free)
2. [Crear la VM Ubuntu 22.04](#2-crear-la-vm-ubuntu-2204)
3. [Abrir puerto 8080 en Oracle](#3-abrir-puerto-8080-en-oracle)
4. [Conectarse por SSH](#4-conectarse-por-ssh)
5. [Instalar el bot](#5-instalar-el-bot)
6. [Configurar .env](#6-configurar-env)
7. [Configurar Telegram (opcional)](#7-configurar-telegram-opcional)
8. [Arrancar el bot](#8-arrancar-el-bot)
9. [Acceder al dashboard](#9-acceder-al-dashboard)
10. [Monitorización y logs](#10-monitorización-y-logs)
11. [Ejecutar el backtest](#11-ejecutar-el-backtest)
12. [Checklist de validación (7-14 días)](#12-checklist-de-validación-7-14-días)
13. [Criterios para ir a producción](#13-criterios-para-ir-a-producción)
14. [Comandos de referencia rápida](#14-comandos-de-referencia-rápida)

---

## 1. Crear cuenta Oracle Cloud Always Free

1. Ve a [cloud.oracle.com](https://cloud.oracle.com) y haz clic en **"Start for free"**
2. Elige **"Always Free"** (no requiere tarjeta de crédito para las VMs gratuitas)
3. Selecciona una región cercana (Europa: Frankfurt, Ámsterdam; US: Ashburn)
4. Completa el registro (nombre, email, contraseña)

> La cuenta Always Free incluye **2 VMs AMD** con 1 OCPU + 1 GB RAM cada una,
> sin límite de tiempo. Son suficientes para el bot.

---

## 2. Crear la VM Ubuntu 22.04

En el panel de Oracle Cloud:

1. **Compute → Instances → Create Instance**
2. Nombre: `polymarket-bot`
3. **Shape**: AMD (VM.Standard.E2.1.Micro) — está en el tier Always Free
4. **Image**: Ubuntu 22.04 LTS
5. **Networking**: VCN por defecto, subnet pública
6. **SSH Keys**: sube tu clave pública (`~/.ssh/id_rsa.pub`) o genera una nueva
7. Clic en **Create**

Espera ~2 minutos a que el estado sea **Running**. Anota la IP pública.

---

## 3. Abrir puerto 8080 en Oracle

Oracle tiene **dos capas de firewall** — hay que abrir el puerto en ambas:

### 3a. Security List (VCN)

1. **Networking → Virtual Cloud Networks → tu VCN → Security Lists → Default**
2. **Add Ingress Rules**:
   - Source CIDR: `0.0.0.0/0`
   - Protocol: TCP
   - Destination Port: `8080`
   - Descripción: "Dashboard bot"
3. Guardar

### 3b. iptables en la VM

El script `setup_oracle.sh` lo hace automáticamente. Si quieres hacerlo manual:

```bash
sudo iptables -I INPUT -p tcp --dport 8080 -j ACCEPT
sudo netfilter-persistent save
```

---

## 4. Conectarse por SSH

```bash
# Desde tu PC (Mac/Linux):
ssh ubuntu@<IP_PUBLICA>

# Si usas Windows: usa PuTTY o WSL con el mismo comando
# La clave SSH que subiste en el paso 2 debe estar en ~/.ssh/
```

---

## 5. Instalar el bot

Una vez dentro del servidor, ejecuta el script de instalación automática:

```bash
# Descargar y ejecutar el setup
bash <(curl -s https://raw.githubusercontent.com/Tucomullen/bot_polymarket/main/deploy/setup_oracle.sh)
```

O manualmente:

```bash
# Clonar el repositorio
git clone https://github.com/Tucomullen/bot_polymarket.git ~/bot
cd ~/bot

# Ejecutar el script de setup
bash deploy/setup_oracle.sh
```

El script instala Python 3.11, crea el virtualenv, instala las dependencias,
configura el servicio systemd y abre el puerto 8080 en iptables.

---

## 6. Configurar .env

```bash
cd ~/bot

# Copiar la plantilla
cp .env.example .env

# Editar con tus valores
nano .env
```

**Mínimo para empezar paper trading:**

```bash
PRIVATE_KEY=tu_clave_privada_aqui_sin_0x
SIMULATION_MODE=true          # ← OBLIGATORIO en true durante paper trading
VERIFY_SSL=true
BANKROLL_USD=1000
DASHBOARD_PORT=8080
```

**Recomendado añadir también:**

```bash
# Risk management
KELLY_FRACTION=0.25
MAX_TOTAL_EXPOSURE_PCT=0.20
MAX_SESSION_LOSS_PCT=0.05
MAX_CONSECUTIVE_ERRORS=10

# Discovery
MAX_ACTIVE_MARKETS=5
DISCOVERY_RESCAN_SEC=300

# Dashboard (proteger el kill switch)
DASHBOARD_PASSWORD=una_contrasena_segura

# Logs
LOG_DIR=logs

# Telegram (ver sección 7)
# TELEGRAM_BOT_TOKEN=...
# TELEGRAM_CHAT_ID=...
```

> **Nunca pongas `SIMULATION_MODE=false` durante paper trading.**
> Cuando quieras operar en live, cambia este valor DESPUÉS de completar
> el checklist de validación completo.

---

## 7. Configurar Telegram (opcional pero recomendado)

Las alertas de Telegram te avisan de kill switch, arranque y resúmenes
sin necesidad de estar mirando el dashboard.

### Crear el bot de Telegram

1. Abre Telegram y busca **@BotFather**
2. Envía `/newbot`
3. Pon un nombre: `PolymarketBot`
4. Pon un username: `mi_polymarket_bot`
5. BotFather te dará un token: `123456789:AAABBBCCC...`

### Obtener tu Chat ID

1. Inicia una conversación con tu bot recién creado
2. Envía cualquier mensaje (por ejemplo `/start`)
3. Busca **@userinfobot** y envíale un mensaje — te dará tu ID numérico

### Configurar en .env

```bash
TELEGRAM_BOT_TOKEN=123456789:AAABBBCCCDDDEEE
TELEGRAM_CHAT_ID=tu_id_numerico
```

---

## 8. Arrancar el bot

```bash
# Iniciar el servicio
sudo systemctl start polymarket-bot

# Verificar que arrancó correctamente
sudo systemctl status polymarket-bot

# Ver logs en tiempo real
sudo journalctl -u polymarket-bot -f
```

El bot debería mostrar:
```
INFO  polybot.main — Config cargada — simulation=True, chain=137
INFO  polybot.main — MODO SIMULACIÓN activado
INFO  polybot.main — Dashboard disponible en http://0.0.0.0:8080
INFO  polybot.disc — Discovery completado — 5 mercados seleccionados
INFO  polybot.loop — Trading loop iniciado — 5 mercados, interval=500ms
```

### Comandos de control del servicio

```bash
sudo systemctl start   polymarket-bot   # arrancar
sudo systemctl stop    polymarket-bot   # parar
sudo systemctl restart polymarket-bot   # reiniciar
sudo systemctl status  polymarket-bot   # estado actual
sudo systemctl enable  polymarket-bot   # auto-arranque al reiniciar el servidor
sudo systemctl disable polymarket-bot   # deshabilitar auto-arranque
```

---

## 9. Acceder al dashboard

Desde tu PC, abre en el navegador:

```
http://<IP_PUBLICA_ORACLE>:8080
```

Verás el dashboard con:
- **Uptime** del bot
- **P&L simulado** en tiempo real
- **Exposición** actual (% del bankroll en órdenes)
- **Mercados activos** con bid/ask en vivo
- **Actividad** (últimas 20 órdenes/fills simulados)
- **Logs** recientes
- **Botón Kill Switch** (cancela todas las órdenes y para el bot)

El dashboard se actualiza automáticamente cada segundo via SSE.

---

## 10. Monitorización y logs

### Logs en tiempo real (consola)

```bash
sudo journalctl -u polymarket-bot -f
```

### Logs JSON estructurados (fichero diario)

```bash
# Ver log de hoy
cat ~/bot/logs/bot_$(date +%Y-%m-%d).log | jq .

# Ver solo errores
cat ~/bot/logs/bot_$(date +%Y-%m-%d).log | jq 'select(.level=="ERROR")'

# Contar fills simulados
cat ~/bot/logs/bot_$(date +%Y-%m-%d).log | jq 'select(.msg | contains("TRADE"))' | wc -l
```

### Endpoint de logs del dashboard

```bash
# Últimas 100 líneas via API
curl http://localhost:8080/api/logs?n=100 | jq '.lines[]'
```

### Estado actual via API

```bash
curl http://localhost:8080/api/status | jq '{pnl: .session_pnl, exp: .exposure_pct, cycles: .cycle_count}'
```

### Ver métricas de riesgo acumuladas

```bash
# El bot imprime stats de riesgo cada 100 ciclos (~50 segundos)
sudo journalctl -u polymarket-bot | grep "Riesgo"
```

---

## 11. Ejecutar el backtest

Con datos históricos reales (requiere conexión a internet en el servidor):

```bash
cd ~/bot
source venv/bin/activate

# Backtest estándar: 30 días, 5 mercados top
python backtesting/run_backtest.py --days 30 --markets 5

# Backtest extendido: 60 días, bankroll $500
python backtesting/run_backtest.py --days 60 --markets 3 --bankroll 500

# Sin informe HTML (solo consola)
python backtesting/run_backtest.py --no-report
```

El informe HTML se genera en `backtesting/report_YYYY-MM-DD.html`.
Para verlo, cópialo a tu PC:

```bash
# Desde tu PC:
scp ubuntu@<IP_ORACLE>:~/bot/backtesting/report_*.html ~/Desktop/
```

---

## 12. Checklist de validación (7-14 días)

Completa todos los puntos antes de considerar ir a producción con capital real.

### Estabilidad

- [ ] El bot corre **72 horas sin reiniciar** solo (ver `uptime` en el dashboard)
- [ ] El servicio se reinicia automáticamente si cae (probar: `sudo kill -9 $(pgrep -f main.py)`)
- [ ] El WebSocket se reconecta tras simular una caída de red:
  ```bash
  # Bloquear temporalmente la conexión al WebSocket de Polymarket
  sudo iptables -I OUTPUT -d ws-subscriptions-clob.polymarket.com -j DROP
  sleep 60
  sudo iptables -D OUTPUT -d ws-subscriptions-clob.polymarket.com -j DROP
  # Verificar que el bot se reconecta solo en el log
  ```

### Re-scan

- [ ] El re-scan periódico actualiza los mercados correctamente cada 5 min
  (buscar en logs: `Re-scan periódico de mercados`)
- [ ] Tras un re-scan, el bot cotiza los nuevos mercados sin reiniciar

### Kill switch

- [ ] El kill switch se activa correctamente desde el dashboard (botón)
- [ ] Tras activarlo, el bot para de cotizar y no genera nuevas órdenes simuladas
- [ ] Se recibe la alerta de Telegram de kill switch (si está configurado)

### Dashboard

- [ ] El dashboard muestra datos coherentes con los logs del servidor
- [ ] Las métricas (P&L, exposición) son consistentes entre `/api/status` y el dashboard
- [ ] Los logs del dashboard coinciden con `journalctl`

### Alertas Telegram

- [ ] Alerta de arranque recibida al iniciar el bot
- [ ] Alerta de kill switch recibida al activarlo
- [ ] (Opcional) Resumen diario recibido tras 24h de ejecución

### P&L simulado

- [ ] El P&L simulado acumulado **tiene el mismo orden de magnitud** que el backtest
  (no es idéntico — el backtest es con datos históricos, el paper trading es en tiempo real)
- [ ] No hay pérdidas excepcionales ni comportamiento anómalo del sizing

### Memoria y recursos

- [ ] No hay memory leaks tras **7 días de ejecución**:
  ```bash
  # Comprobar memoria usada por el proceso
  ps aux | grep "main.py" | awk '{print $6}' # en KB
  # Si crece continuamente durante días → memory leak
  ```
- [ ] El proceso no supera el 80% de la RAM disponible (896 MB de 1 GB)

---

## 13. Criterios para ir a producción

**Solo pasa a `SIMULATION_MODE=false` si TODO lo siguiente es verdad:**

| Criterio | Verificado |
|----------|-----------|
| Fase 5a: 95/95 unit tests pasan | ✅ Completado |
| Fase 5b: Backtest con Sharpe > 0.5 y P&L positivo | Verificar con datos reales |
| Fase 5c: Dashboard funciona + alertas Telegram | ✅ Completado |
| Fase 5d: 7+ días sin crash | Pendiente paper trading |
| P&L simulado coherente con backtest | Pendiente paper trading |
| Kill switch verificado manualmente | Pendiente paper trading |
| Cuenta de Polygon con USDC depositado en Polymarket | Pendiente |
| `.env` revisado: `SIMULATION_MODE=false`, `VERIFY_SSL=true` | Pendiente |

### Activar modo live

```bash
# En el servidor:
nano ~/bot/.env
# Cambiar: SIMULATION_MODE=true → SIMULATION_MODE=false
# Guardar

sudo systemctl restart polymarket-bot

# Verificar que arranca en modo live
sudo journalctl -u polymarket-bot -f
# Debe aparecer: "simulation=False" y NO "MODO SIMULACIÓN"
```

---

## 14. Comandos de referencia rápida

```bash
# Estado del bot
sudo systemctl status polymarket-bot

# Logs en tiempo real
sudo journalctl -u polymarket-bot -f

# Logs de los últimos 30 minutos
sudo journalctl -u polymarket-bot --since "30 minutes ago"

# Reiniciar tras cambios en .env
sudo systemctl restart polymarket-bot

# Actualizar código desde GitHub
bash ~/bot/deploy/update.sh

# Estado del dashboard
curl http://localhost:8080/api/status | jq .

# Uso de memoria del proceso
ps aux | grep main.py

# Espacio en disco
df -h ~/bot/logs/

# Rotar logs manualmente (si crecen demasiado)
truncate -s 0 ~/bot/logs/bot_$(date +%Y-%m-%d).log
```

---

*Documento creado: Abril 2026*
*Fases completadas: 5a ✅ · 5b ✅ · 5c ✅ · 5d ⏳ (paper trading pendiente)*

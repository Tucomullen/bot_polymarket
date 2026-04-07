# 📋 Plan Pre-Producción — Bot de Trading Polymarket

**Objetivo:** Validar exhaustivamente el bot antes de contratar el droplet de producción en DigitalOcean.

> Este documento define las 4 fases de validación + el entorno gratuito de pruebas. Cada fase tiene criterios de éxito claros. Solo se pasa a producción cuando las 4 fases están superadas.

---

## Resumen ejecutivo

```
Fase 5a — Unit Tests        → Validar que cada módulo funciona correctamente en aislamiento
Fase 5b — Backtesting       → Validar que la estrategia es rentable sobre datos históricos
Fase 5c — Dashboard         → Visibilidad total: web dashboard + alertas Telegram
Fase 5d — Oracle Cloud      → Paper trading extendido (1-2 semanas) en entorno gratuito
              ↓
           PRODUCCIÓN       → DigitalOcean Droplet (solo si 5a + 5b + 5c + 5d OK)
```

---

## Entorno de pruebas gratuito

### ¿Por qué Oracle Cloud Always Free?

| Opción | Coste | Duración | RAM | Veredicto |
|--------|-------|----------|-----|-----------|
| **Oracle Cloud Always Free** | Gratis para siempre | Sin límite | 1 GB / 1 OCPU × 2 VMs | ✅ **Elegida** |
| GitHub Codespaces | 60 h/mes gratis | Mientras corre | 2 GB / 2 cores | Para pruebas cortas |
| Render.com free | Gratis | Se apaga sin uso | 512 MB | Demasiado limitado |
| DigitalOcean free trial | $200 crédito / 60 días | 2 meses | A elección | Válido pero expira |

**Oracle Cloud Always Free** ofrece la misma experiencia que DigitalOcean sin coste ni fecha de expiración. Ideal para el período de validación de 1-2 semanas.

---

## FASE 5a — Unit Tests

### Objetivo
Verificar en aislamiento que cada módulo produce los resultados esperados, especialmente los cálculos de riesgo, cotización y scoring que afectan directamente al capital.

### Módulos a cubrir

| Módulo | Tests clave |
|--------|-------------|
| `src/quoting.py` | Kelly sizing con distintos spreads y bankrolls; protección anti-cross; quantización al tick; skew por inventario |
| `src/risk_manager.py` | Kill switch por pérdida y por errores; cálculo de exposición total; P&L al registrar fills BUY/SELL |
| `src/orderbook.py` | Deadlock no ocurre (lock no reentrante); midpoint y spread calculados correctamente |
| `src/discovery.py` | Filtros duros; scoring ponderado; clasificación de categorías |
| `config/settings.py` | Variables obligatorias faltan → error claro; defaults correctos |

### Herramientas
- **pytest** — framework de tests
- **pytest-asyncio** — para corrutinas async (discovery, order_manager)
- **respx** — mock de httpx para simular respuestas de la API sin red real

### Estructura de archivos
```
tests/
├── conftest.py              # Fixtures compartidos (market candidato, config mock, etc.)
├── test_quoting.py          # QuotingEngine
├── test_risk_manager.py     # RiskManager
├── test_orderbook.py        # OrderbookTracker
├── test_discovery.py        # MarketDiscovery (filtros y scoring)
└── test_settings.py         # Config loading
```

### Criterio de éxito
- ✅ 100% de los tests pasan
- ✅ Sin warnings de deprecation
- ✅ Cobertura > 80% en los módulos core (quoting, risk_manager, orderbook)

---

## FASE 5b — Backtesting

### Objetivo
Validar que la estrategia de market making genera P&L positivo sobre datos históricos reales antes de arriesgar capital real.

### Fuente de datos
La **Gamma API** de Polymarket ofrece:
- `GET /markets/{conditionId}/prices-history?interval=1h` → precio cada hora
- `GET /trades?market={conditionId}&limit=500` → trades históricos reales

### Metodología de simulación

```
Para cada mercado del top-5 del Discovery:
  1. Descargar 30-60 días de precio histórico (mid-price por hora)
  2. En cada punto temporal:
     a. Pasar el mid-price al QuotingEngine → obtener bid/ask
     b. Comparar con el precio del siguiente período
     c. Si el precio bajó por debajo de nuestro bid → fill BUY simulado
     d. Si el precio subió por encima de nuestro ask → fill SELL simulado
  3. Acumular posición, P&L y drawdown
  4. Calcular métricas finales
```

> **Limitación conocida:** los datos son por hora, no tick a tick. El backtest es una aproximación válida para validar la rentabilidad direccional de la estrategia, pero no refleja el comportamiento exacto a 500ms.

### Métricas de salida

| Métrica | Umbral mínimo para continuar |
|---------|------------------------------|
| **P&L neto** | > 0 (positivo) |
| **Sharpe ratio** | > 0.5 |
| **Max drawdown** | < 15% del bankroll |
| **Fill rate** | > 5% (alguna orden se llena) |
| **Días rentables** | > 60% del período |

### Estructura de archivos
```
backtesting/
├── downloader.py     # Descarga datos históricos de Gamma API
├── simulator.py      # Motor de simulación (replay de precios + fills)
├── metrics.py        # Cálculo de Sharpe, drawdown, P&L acumulado
├── report.py         # Genera informe HTML + gráficas (matplotlib)
└── run_backtest.py   # Script principal: descarga + simula + reporta
```

### Salida esperada
```bash
python backtesting/run_backtest.py --days 30 --markets 5

📈 Backtest completado — 30 días, 5 mercados
   P&L neto:      +$47.23  (+4.7%)
   Sharpe ratio:  0.82
   Max drawdown:  -$31.10  (-3.1%)
   Fill rate:     12.4%
   Días rentables: 18/30 (60%)

   Informe guardado en: backtesting/report_2026-04-06.html
```

---

## FASE 5c — Dashboard y Alertas

### Objetivo
Visibilidad completa del estado del bot en tiempo real desde cualquier dispositivo, con alertas automáticas para eventos críticos.

### Dashboard Web (FastAPI + SSE)

**Tecnología:**
- **FastAPI** — servidor HTTP ligero (Python)
- **Server-Sent Events (SSE)** — push de datos al navegador sin polling
- **HTML + CSS vanilla** — sin frameworks JS, máxima simplicidad

**Pantallas:**

```
┌─────────────────────────────────────────────────────┐
│  POLYMARKET BOT — Dashboard                  🟢 LIVE │
├──────────────┬──────────────┬──────────────┬────────┤
│ Uptime       │ P&L sesión   │ Exposición   │ KS     │
│ 2h 34min     │ +$3.42       │ 12.3%        │  ✅    │
├──────────────┴──────────────┴──────────────┴────────┤
│ MERCADOS ACTIVOS                                     │
│  1. BTC 5-min up/down    bid=0.478 ask=0.522  ✅    │
│  2. ETH 15-min above     bid=0.391 ask=0.441  ✅    │
│  3. ...                                             │
├─────────────────────────────────────────────────────┤
│ ACTIVIDAD (últimas 20 órdenes)                      │
│  22:44:12  BUY  YES  0.478  52sh  BTC 5-min  SIM   │
│  22:44:12  SELL YES  0.522  48sh  BTC 5-min  SIM   │
│  ...                                                │
├─────────────────────────────────────────────────────┤
│ LOGS RECIENTES                       [🛑 KILL SW]   │
│  22:45:04 Ciclo #100 — 0.5ms ...                   │
│  22:45:04 Riesgo — PnL=0.0000 ...                  │
└─────────────────────────────────────────────────────┘
```

**Endpoints:**
- `GET /` → dashboard HTML
- `GET /api/status` → JSON con métricas actuales
- `GET /api/stream` → SSE stream de actualizaciones en tiempo real
- `POST /api/kill-switch` → activar kill switch manualmente
- `GET /api/logs` → últimas N líneas de log

### Alertas Telegram

**Eventos que disparan alerta:**
- 🚨 Kill switch activado (con motivo)
- ⚠️ Bot caído / error fatal
- 📊 Resumen diario de P&L (cada 24h)
- ✅ Bot arrancado correctamente
- 🔄 Re-scan detectó cambio de mercados

**Setup:** bot de Telegram gratuito vía @BotFather. Solo requiere `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en `.env`.

### Logging persistente

Logs estructurados en JSON guardados en archivo rotante:
```
logs/
├── bot_2026-04-06.log    # Log del día (JSON lines)
├── bot_2026-04-05.log
└── ...
```

Cada línea: `{"ts": 1744000000, "level": "INFO", "module": "risk", "msg": "...", "data": {...}}`

### Estructura de archivos
```
src/
├── dashboard.py          # FastAPI app con endpoints y SSE
└── telegram_alerts.py    # Cliente Telegram para notificaciones
```

### Criterio de éxito
- ✅ Dashboard accesible en `http://[ip]:8080` desde el navegador
- ✅ Métricas se actualizan en tiempo real (< 2s de latencia)
- ✅ Alerta de Telegram llega en < 10s tras kill switch
- ✅ Logs persistentes entre reinicios del bot

---

## FASE 5d — Oracle Cloud + Paper Trading Extendido

### Objetivo
Ejecutar el bot en modo simulación durante 1-2 semanas en un entorno de servidor real (24h/día) para:
- Detectar errores que solo aparecen con días de ejecución continuada (memory leaks, reconexiones, etc.)
- Validar que el re-scan periódico funciona en producción
- Acumular datos de P&L simulado suficientes para tomar la decisión de ir a live

### Setup de Oracle Cloud Always Free

**Pasos:**
1. Crear cuenta en [cloud.oracle.com](https://cloud.oracle.com) (no requiere tarjeta si se elige "Always Free")
2. Crear VM: **AMD shape**, 1 OCPU, 1 GB RAM, Ubuntu 22.04
3. Abrir puerto 8080 (dashboard) en el security group
4. Transferir proyecto (sin `.env` — crearlo manualmente en el servidor)
5. Instalar dependencias + configurar systemd para auto-arranque

**Diferencias con el VPS de producción (DigitalOcean):**
- Oracle Always Free: 1 OCPU, 1 GB RAM (suficiente para simulación)
- DigitalOcean producción: 2 vCPU, 4 GB RAM, NYC (latencia mínima con Polymarket)

> Oracle es solo para validación. Para producción en live se usará DigitalOcean por la latencia y la garantía de SLA.

### Checklist de validación (1-2 semanas de paper trading)

```
□ El bot corre 72h sin reiniciar solo
□ El re-scan periódico actualiza los mercados correctamente
□ El WebSocket se reconecta tras una caída simulada (kill -SIGTERM + restart)
□ El kill switch se activa correctamente en la condición configurada
□ El dashboard muestra datos coherentes con los logs
□ Las alertas de Telegram llegan para todos los eventos críticos
□ El P&L simulado es consistente con el backtest (mismo orden de magnitud)
□ No hay memory leaks tras 168h (1 semana) de ejecución
```

### Criterio de éxito para ir a PRODUCCIÓN

- ✅ Fase 5a: 100% unit tests pasan
- ✅ Fase 5b: backtest con Sharpe > 0.5 y P&L positivo
- ✅ Fase 5c: dashboard operativo + alertas Telegram funcionando
- ✅ Fase 5d: 7 días consecutivos sin crash, métricas coherentes
- ✅ Revisión manual del P&L simulado acumulado

---

## Resumen de archivos nuevos por fase

| Fase | Archivos nuevos | Dependencias nuevas |
|------|-----------------|---------------------|
| 5a | `tests/conftest.py`, `tests/test_*.py` (5 archivos) | `pytest`, `pytest-asyncio`, `respx` |
| 5b | `backtesting/downloader.py`, `simulator.py`, `metrics.py`, `report.py`, `run_backtest.py` | `matplotlib`, `pandas` |
| 5c | `src/dashboard.py`, `src/telegram_alerts.py` | `fastapi`, `uvicorn`, `jinja2` |
| 5d | Guía de setup Oracle Cloud | — |

---

## Timeline estimado

| Fase | Duración de implementación | Duración de validación |
|------|---------------------------|------------------------|
| 5a — Unit Tests | ~3h | Inmediata (CI) |
| 5b — Backtesting | ~4h | ~1h (ejecutar + revisar) |
| 5c — Dashboard | ~5h | ~30min (probar en local) |
| 5d — Oracle + Paper trading | ~1h setup | **7-14 días** |
| **Total** | **~13h de desarrollo** | **1-2 semanas** |

---

## Estado actual

| Fase | Estado |
|------|--------|
| 1 — Conexión, Auth, WebSocket, Discovery | ✅ Completada |
| 2 — Estrategia Maker + Trading Loop | ✅ Completada |
| SSL — Bypass proxy corporativo | ✅ Completada |
| 3 — Fee-rate real + Re-scan periódico | ✅ Completada |
| 4 — Gestión de Riesgo (Kill switch + Kelly) | ✅ Completada |
| **5a — Unit Tests** | ✅ Completada (95/95 tests pasan) |
| **5b — Backtesting** | ✅ Completada (backtesting/ con 5 módulos) |
| **5c — Dashboard + Alertas** | ⏳ Pendiente |
| **5d — Oracle Cloud + Paper Trading** | ⏳ Pendiente |
| **Producción (DigitalOcean)** | 🔒 Bloqueada hasta 5a+5b+5c+5d |

---

*Documento creado: Abril 2026*

# 🤖 Guía Paso a Paso — Bot de Trading Polymarket

**Estado actual: FASE 4 completada (Gestión de Riesgo)**

---

## FASE 4 — Lo que se ha añadido

### Archivos nuevos

| Archivo | Qué hace |
|---------|----------|
| `src/risk_manager.py` | **Risk Manager** — kill switch, Kelly sizing, exposición por mercado, P&L de sesión |

### Archivos modificados

| Archivo | Cambio |
|---------|--------|
| `src/quoting.py` | `_compute_order_size` usa Kelly fraccional si hay `risk_manager` |
| `src/trading_loop.py` | Comprueba kill switch en cada ciclo; registra fills en el risk manager; logs de riesgo cada 100 ciclos |
| `main.py` | Crea `RiskManager` con config de `.env` y lo pasa al `TradingLoop` |
| `.env.example` | Añadidas `MAX_TOTAL_EXPOSURE_PCT`, `MAX_SESSION_LOSS_PCT`, `MAX_CONSECUTIVE_ERRORS` |

---

## Qué implementa la Fase 4

### 1. Kill Switch (parada de emergencia)

El bot se detiene y cancela todas las órdenes automáticamente si:

| Condición | Variable | Default |
|-----------|----------|---------|
| Pérdida de sesión > X% del bankroll | `MAX_SESSION_LOSS_PCT` | 5% |
| X errores consecutivos de API | `MAX_CONSECUTIVE_ERRORS` | 10 |

Al activarse, el log muestra:
```
🚨 KILL SWITCH activado: pérdida de sesión $52.00 supera límite $50.00
🛑 KILL SWITCH activo (...) — deteniendo el bot
```

### 2. Kelly Fraccional

El tamaño de cada orden ya no es fijo ni solo escala por score. Ahora aplica la fórmula Kelly adaptada a market making:

```
edge = half_spread_cents / 100 / mid_price
kelly_size = bankroll × kelly_fraction × edge × 2
```

**Ejemplo:** bankroll=$1000, Kelly=0.25, mid=0.50, spread=2¢
```
edge = 0.01 / 0.50 = 2%
kelly_size = 1000 × 0.25 × 0.02 × 2 = $10 por orden
```

El tamaño final se limita siempre por:
- `MAX_BANKROLL_RISK_PCT` (hard cap por orden individual, default 5%)
- Capacidad restante hasta `MAX_TOTAL_EXPOSURE_PCT` del bankroll

### 3. Control de Exposición Total

El bot rastrea cuánto capital hay comprometido en órdenes vivas en todo momento. Si la suma de todas las órdenes abiertas supera `MAX_TOTAL_EXPOSURE_PCT × bankroll`, el sizing Kelly devuelve 0 y no se abren más órdenes hasta que alguna se llene o cancele.

**Default:** máx 20% del bankroll en órdenes vivas simultáneamente.

### 4. P&L Tracking

El Risk Manager registra cada fill del WebSocket user channel y calcula:
- **Posición neta** por mercado (shares YES acumuladas)
- **Precio medio de entrada** ponderado
- **P&L realizado** por spread capturado (SELL fill - avg_entry_price × size)
- **P&L total de sesión** (suma de todos los mercados)

### 5. Log periódico de riesgo (cada 100 ciclos)

```
📊 Riesgo — PnL=0.0042 | Exposición=8.3% ($83.00) | Errores=0 | KS=✅ | Uptime=12.3min
```

---

## Variables de entorno nuevas

| Variable | Default | Descripción |
|----------|---------|-------------|
| `KELLY_FRACTION` | 0.25 | Fracción del Kelly óptimo (0.25=conservador, 0.5=moderado) |
| `MAX_BANKROLL_RISK_PCT` | 0.05 | Hard cap por orden = 5% del bankroll |
| `MAX_TOTAL_EXPOSURE_PCT` | 0.20 | Máx capital en órdenes vivas = 20% |
| `MAX_SESSION_LOSS_PCT` | 0.05 | Kill switch al perder 5% del bankroll en la sesión |
| `MAX_CONSECUTIVE_ERRORS` | 10 | Kill switch tras 10 errores consecutivos de API |

---

## Tareas manuales para Fase 4

**⏳ TAREA: Revisar y ajustar los parámetros de riesgo en `.env`**

Valores recomendados para empezar:
```
KELLY_FRACTION=0.25          # Conservador para empezar
MAX_BANKROLL_RISK_PCT=0.05   # Máx $50 por orden en bankroll de $1000
MAX_TOTAL_EXPOSURE_PCT=0.20  # Máx $200 en órdenes vivas
MAX_SESSION_LOSS_PCT=0.05    # Kill switch si pierdes $50 en la sesión
MAX_CONSECUTIVE_ERRORS=10    # Kill switch si hay 10 errores seguidos
```

Para un perfil más agresivo (solo tras validar en simulación):
```
KELLY_FRACTION=0.5
MAX_TOTAL_EXPOSURE_PCT=0.30
MAX_SESSION_LOSS_PCT=0.10
```

**⏳ TAREA: Ejecutar en simulación y verificar logs de riesgo**

```bash
python main.py
```

Deberías ver al arrancar:
```
🛡️  Risk Manager — Kelly=0.25 | max_order=5% | max_exposure=20% | kill_loss=5%
```

Y cada 100 ciclos (~50s a 500ms/ciclo):
```
📊 Riesgo — PnL=0.0000 | Exposición=0.0% ($0.00) | Errores=0 | KS=✅ | Uptime=0.8min
```

En simulación el P&L siempre será 0 (las órdenes no se llenan realmente). En live, este número reflejará el spread capturado.

---

## Cómo funciona el ciclo ahora (completo)

```
Cada 500ms:
  1. ¿Kill switch activo? → cancelar todo y parar
  2. Para cada mercado:
     a. ¿Hay que recotizar?
     b. Kelly sizing: bankroll × kelly_fraction × edge × 2
     c. Limitar por max_order_pct y exposición disponible
     d. Generar bid+ask
     e. Sync con órdenes vivas
  3. Cada 100 ciclos → log de riesgo

WebSocket user channel (fills en live):
  → Actualizar posición y P&L en risk manager
  → Si pérdida > MAX_SESSION_LOSS_PCT → kill switch en próximo ciclo
```

---

## Resumen completo de archivos

| Archivo | Fase | Descripción |
|---------|------|-------------|
| `.env.example` | 1+2+3+4 | Plantilla de configuración completa |
| `config/settings.py` | 1 | Carga config (incluye RiskConfig con kelly_fraction) |
| `src/auth.py` | 1 | Autenticación L1+L2 |
| `src/websocket_manager.py` | 1+SSL | WebSocket con heartbeat, reconexión y SSL bypass |
| `src/orderbook.py` | 1 | Estado local del libro de órdenes |
| `src/discovery.py` | 1+3 | Selección automática + enriquecimiento real de fee-rate/spread |
| `src/quoting.py` | 2+**4** | Motor de cotización + Kelly sizing |
| `src/order_manager.py` | 2 | Gestión de órdenes + fee-rate dinámico |
| `src/risk_manager.py` | **4** | Kill switch, Kelly, exposición, P&L |
| `src/trading_loop.py` | 2+**4** | Bucle de trading + integración kill switch |
| `main.py` | 1+2+SSL+3+**4** | Punto de entrada + Risk Manager |

---

## Próximas fases

| Fase | Estado | Qué implementa |
|------|--------|-----------------|
| 1 | ✅ | Auth L1/L2, WebSocket, Heartbeat, Market Selector |
| 2 | ✅ | Cotización bidireccional, cancel/replace, fee-rate dinámico |
| SSL | ✅ | Bypass SSL para proxy corporativo |
| 3 | ✅ | Enriquecimiento real de scoring, re-scan periódico |
| 4 | ✅ | Kill switch, Kelly fraccional, exposición, P&L tracking |
| 5 | ⏳ | Monitorización avanzada: dashboard, alertas, auto-restart |

---

*Última actualización: Fase 4 — Abril 2026*

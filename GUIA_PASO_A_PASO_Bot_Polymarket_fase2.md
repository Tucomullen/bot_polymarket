# 🤖 Guía Paso a Paso — Bot de Trading Polymarket

**Estado actual: FASE 2 completada (Estrategia Maker + Bucle de Velocidad)**

---

## FASE 2 — Lo que se ha añadido

### Archivos nuevos

| Archivo | Qué hace |
|---------|----------|
| `src/quoting.py` | **Quoting Engine** — genera cotizaciones bid+ask simultáneas con spread dinámico |
| `src/order_manager.py` | **Order Manager** — crea, cancela y reemplaza órdenes con fee-rate dinámico |
| `src/trading_loop.py` | **Trading Loop** — orquesta el ciclo continuo de market making |

### Archivos modificados

| Archivo | Cambio |
|---------|--------|
| `main.py` | Integra el Trading Loop; lo lanza en paralelo al WebSocket |
| `.env.example` | Añadidos `BANKROLL_USD` y `CYCLE_INTERVAL_MS` |

### Cómo funciona el ciclo de trading

Cada 500ms (configurable con `CYCLE_INTERVAL_MS`), el bot ejecuta este ciclo para cada mercado activo:

1. **¿Recotizar?** — Comprueba si el midpoint se movió >0.5¢, si la quote tiene >5s, o si no hay quote
2. **Generar quotes** — El Quoting Engine calcula bid y ask con:
   - Anclaje al midpoint del orderbook en tiempo real
   - Spread dinámico (se estrecha si hay rewards con max_spread, se ensancha con inventario)
   - Skew por inventario (si tenemos mucho YES, baja el bid para dejar de comprar YES)
   - Tamaño escalado por score del mercado y bankroll
   - Protección anti-cross (nunca cruza el spread accidentalmente)
   - Cuantización al tick size del mercado
3. **Sync órdenes** — El Order Manager compara las quotes nuevas con las órdenes vivas:
   - Si coinciden (precio similar ±0.5¢) → no hace nada
   - Si difieren → cancela la vieja + crea la nueva
   - Si no hay orden → crea
4. **Fee rate dinámico** — Antes de crear CUALQUIER orden, consulta `GET /fee-rate?tokenID=...` y lo incluye en la firma. Cache de 10s para no saturar la API.
5. **Procesamiento de fills** — Si el WebSocket notifica un fill, actualiza el inventario local

### Tareas manuales para Fase 2

**⏳ TAREA: Añadir variables a tu .env**

```
BANKROLL_USD=1000
CYCLE_INTERVAL_MS=500
```

- `BANKROLL_USD`: tu capital total. El bot nunca arriesga más del 5% por orden.
- `CYCLE_INTERVAL_MS`: intervalo entre ciclos. 500ms es conservador. En VPS puedes bajar a 200ms.

**⏳ TAREA: Ejecutar y observar**

```bash
python main.py
```

Ahora verás líneas adicionales en el log:

```
12:00:05 │ polybot.loop       │ INFO  │ 🔄 Trading loop iniciado — 5 mercados, interval=500ms, simulation=True
12:00:07 │ polybot.quoting    │ INFO  │ 📐 Quote generada — BTC 5-min up or down | bid=0.478 (52 sh) | ask=0.522 (48 sh) | mid=0.500 | spread=4.4¢
12:00:07 │ polybot.orders     │ INFO  │ 📝 [SIMULACIÓN] Orden creada — BUY YES @ 0.478 × 52 sh | fee=100 bps
12:00:07 │ polybot.orders     │ INFO  │ 📝 [SIMULACIÓN] Orden creada — SELL YES @ 0.522 × 48 sh | fee=100 bps
...
12:00:57 │ polybot.loop       │ INFO  │ 📊 Ciclo #100 — 2.3ms | created=12, cancelled=8, fills=0, errors=0
```

**Todo sigue en modo simulación** — las órdenes se logean pero NO se envían al CLOB. Esto es seguro.

---

## Resumen completo de archivos

| Archivo | Fase | Descripción |
|---------|------|-------------|
| `.env.example` | 1+2 | Plantilla de configuración |
| `config/settings.py` | 1 | Carga config desde .env |
| `src/auth.py` | 1 | Autenticación L1+L2 |
| `src/websocket_manager.py` | 1 | WebSocket con heartbeat y reconexión |
| `src/orderbook.py` | 1 | Estado local del libro de órdenes |
| `src/discovery.py` | 1 | Selección automática de mercados |
| `src/quoting.py` | **2** | Motor de cotización bidireccional |
| `src/order_manager.py` | **2** | Gestión de órdenes + fee-rate dinámico |
| `src/trading_loop.py` | **2** | Bucle de trading que orquesta todo |
| `main.py` | 1+2 | Punto de entrada |

---

## Próximas fases

| Fase | Prompt | Estado | Qué implementa |
|------|--------|--------|-----------------|
| 1 | Conexión, Auth + Discovery | ✅ | Auth L1/L2, WebSocket, Heartbeat, Market Selector |
| 2 | Estrategia Maker + Velocidad | ✅ | Cotización bidireccional, cancel/replace, fee-rate dinámico |
| 3 | Tarifas y Firmas | ⏳ | Refinar firma con feeRateBps (ya integrado en Order Manager) |
| 4 | Gestión de Riesgo | ⏳ | Auto-hedge, Kelly fraccional, kill switch, límites de exposición |

**Nota sobre Fase 3:** La consulta dinámica de `feeRateBps` ya está implementada en el Order Manager de esta Fase 2 (método `_get_fee_rate`). La Fase 3 puede enfocarse en refinar la firma criptográfica del payload si hay issues específicos.

---

*Última actualización: Fase 2 — Abril 2026*
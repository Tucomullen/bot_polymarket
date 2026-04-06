# 🤖 Guía Paso a Paso — Bot de Trading Polymarket

**Estado actual: FASE 3 completada (Tarifas, Firmas y Re-scan Periódico)**

---

## FASE 3 — Lo que se ha añadido

### Archivos modificados

| Archivo | Cambio |
|---------|--------|
| `src/discovery.py` | `_enrich_candidates` ahora llama realmente a `_fetch_fee_rate` + `_fetch_top_of_book` con semáforo (máx 5 llamadas paralelas) |
| `main.py` | Re-scan periódico cada 5 min que actualiza la lista de mercados del Trading Loop; `discovery` se mantiene vivo durante toda la sesión |
| `.env.example` | Añadida variable `DISCOVERY_RESCAN_SEC` |
| `.env` | Añadida variable `DISCOVERY_RESCAN_SEC=300` |

---

## Qué problemas resuelve la Fase 3

### Problema 1 — Scoring de mercados incompleto (crítico)

Antes de Fase 3, `_enrich_candidates` era un placeholder vacío. Esto significaba:

- `has_maker_rebates` siempre `False` → el 15% del peso del scoring nunca se usaba
- `has_taker_fees` siempre `False`
- El spread en el scoring venía de la Gamma API (puede tener varios minutos de retraso)

**Ahora:** El enriquecimiento consulta en paralelo (con semáforo de 5) el endpoint `/fee-rate` y el orderbook `/book` del CLOB para cada candidato que pasa los filtros duros. El scoring es real.

### Problema 2 — Mercados obsoletos al cabo de horas

Sin re-scan, el bot operaba indefinidamente en los mismos 5 mercados del arranque, aunque:
- Alguno se hubiera resuelto
- Hubieran aparecido mejores mercados con más rewards
- El spread de un mercado hubiera empeorado mucho

**Ahora:** Cada `DISCOVERY_RESCAN_SEC` segundos (default 300s = 5 min), el bot lanza un nuevo scan completo y actualiza la lista de mercados del Trading Loop sin reiniciar.

### Validación de la firma (feeRateBps)

Se confirmó que `py-clob-client >= 0.34.6` acepta `fee_rate_bps` directamente en `OrderArgs`. La firma criptográfica en modo live ya incluye el fee rate dinámico tal como exige el CLOB. No se requirieron cambios en `order_manager.py`.

---

## Cómo funciona ahora el ciclo completo

```
Arranque
  ├── Discovery inicial → top 5 mercados (con fee-rate y spread reales)
  ├── WebSocket → orderbook en tiempo real
  └── Trading Loop → ciclo cada 500ms

Cada 5 minutos (en paralelo)
  └── Re-scan discovery → si cambia el ranking, actualiza mercados del loop
```

---

## Tareas manuales para Fase 3

**⏳ TAREA: Ejecutar y verificar el enriquecimiento**

```bash
python main.py
```

Ahora deberías ver líneas nuevas durante el discovery:

```
12:00:03 │ polybot.discovery  │ INFO  │    💸 33 candidatos enriquecidos — 8 con maker rebates
12:00:03 │ polybot.discovery  │ INFO  │    1. [82.4] BTC 5-min up or down — spread=1.1¢, rebates=✅, rewards=✅
...
12:05:03 │ polybot.main       │ INFO  │ 🔍 Re-scan periódico de mercados...
12:05:08 │ polybot.main       │ INFO  │ ✅ Re-scan completado — 5 mercados activos
```

**⏳ TAREA (opcional): Ajustar intervalo de re-scan**

En `.env`:
```
DISCOVERY_RESCAN_SEC=300   # 5 min (recomendado para VPS)
DISCOVERY_RESCAN_SEC=600   # 10 min (si quieres reducir llamadas a la API)
```

---

## Resumen completo de archivos

| Archivo | Fase | Descripción |
|---------|------|-------------|
| `.env.example` | 1+2+3 | Plantilla de configuración |
| `config/settings.py` | 1 | Carga config desde .env |
| `src/auth.py` | 1 | Autenticación L1+L2 |
| `src/websocket_manager.py` | 1+SSL | WebSocket con heartbeat, reconexión y SSL bypass |
| `src/orderbook.py` | 1 | Estado local del libro de órdenes |
| `src/discovery.py` | 1+**3** | Selección automática + enriquecimiento real de fee-rate/spread |
| `src/quoting.py` | 2 | Motor de cotización bidireccional |
| `src/order_manager.py` | 2 | Gestión de órdenes + fee-rate dinámico en firma |
| `src/trading_loop.py` | 2 | Bucle de trading que orquesta todo |
| `main.py` | 1+2+SSL+**3** | Punto de entrada + re-scan periódico |

---

## Próximas fases

| Fase | Estado | Qué implementa |
|------|--------|-----------------|
| 1 | ✅ | Auth L1/L2, WebSocket, Heartbeat, Market Selector |
| 2 | ✅ | Cotización bidireccional, cancel/replace, fee-rate dinámico |
| SSL | ✅ | Bypass SSL para proxy corporativo (VERIFY_SSL) |
| 3 | ✅ | Enriquecimiento real de scoring, re-scan periódico, firma verificada |
| 4 | ⏳ | Gestión de Riesgo: auto-hedge, Kelly fraccional, kill switch, límites de exposición |

---

*Última actualización: Fase 3 — Abril 2026*

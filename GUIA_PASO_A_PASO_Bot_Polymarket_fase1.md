# 🤖 Guía Paso a Paso — Bot de Trading Polymarket

**Estado actual: FASE 1 completada (Conexión, Autenticación, WebSocket + Discovery)**

> Esta guía se actualiza con cada prompt ejecutado. Los pasos marcados con ✅ ya están implementados en el código. Los marcados con ⏳ son tareas manuales que debes completar tú.

---

## FASE 1 — Infraestructura, Autenticación y Discovery (El Cimiento)

### Paso 1: Preparar tu entorno de desarrollo

**⏳ TAREA MANUAL**

1. Asegúrate de tener **Python 3.9+** instalado:
   ```
   python3 --version
   ```

2. Crea una carpeta para el proyecto y copia allí todos los archivos generados.

3. Crea un entorno virtual:
   ```
   cd polymarket-bot
   python3 -m venv venv
   source venv/bin/activate   # Linux/Mac
   venv\Scripts\activate      # Windows
   ```

4. Instala las dependencias:
   ```
   pip install -r requirements.txt
   ```

---

### Paso 2: Obtener tu clave privada

**⏳ TAREA MANUAL — CRÍTICA DE SEGURIDAD**

**Opción A — MetaMask / wallet de navegador:**
- MetaMask → ícono 3 puntos → Detalles de cuenta → Exportar clave privada.
- `SIGNATURE_TYPE=0` en `.env`

**Opción B — Login por email de Polymarket (Magic):**
- Ve a [https://reveal.magic.link/polymarket](https://reveal.magic.link/polymarket)
- `SIGNATURE_TYPE=1` en `.env`
- `FUNDER_ADDRESS` = tu dirección de perfil de Polymarket

> ⛔ **NUNCA** compartas tu clave privada.

---

### Paso 3: Configurar el archivo .env

**⏳ TAREA MANUAL**

1. Copia el archivo de ejemplo:
   ```
   cp .env.example .env
   ```

2. Rellena los campos obligatorios:
   - `PRIVATE_KEY` → tu clave privada (sin prefijo `0x`)
   - `FUNDER_ADDRESS` → solo si usas Magic/Email
   - `SIGNATURE_TYPE` → `0` para EOA, `1` para Magic
   - `SIMULATION_MODE=true` → **DÉJALO EN TRUE**

3. **NUEVO — Sobre los Token IDs (ahora OPCIONALES):**
   - Si **dejas vacíos** `TARGET_TOKEN_ID_YES`, `TARGET_TOKEN_ID_NO` y `CONDITION_ID`, el bot activará el **Discovery automático** y seleccionará los mejores mercados solo.
   - Si **los rellenas**, el bot usará esos mercados específicos (modo manual).
   - `MAX_ACTIVE_MARKETS=5` controla cuántos mercados opera simultáneamente en modo Discovery.

4. Las credenciales L2 se rellenan automáticamente en la primera ejecución.

---

### Paso 4: (AHORA OPCIONAL) Obtener Token IDs manualmente

**⏳ TAREA MANUAL — Solo si quieres operar mercados específicos**

El Discovery automático hace esto por ti. Pero si prefieres elegir tú:

1. Endpoint: `https://gamma-api.polymarket.com/markets?active=true&limit=10`
2. Anota `tokens[0].token_id` → `TARGET_TOKEN_ID_YES`
3. Anota `tokens[1].token_id` → `TARGET_TOKEN_ID_NO`
4. Anota `condition_id` → `CONDITION_ID`

---

### Paso 5: Configurar Token Allowances (solo EOA)

**⏳ TAREA MANUAL — Solo si SIGNATURE_TYPE=0**

Realiza al menos un trade manual pequeño ($1) en la UI de Polymarket para configurar los allowances automáticamente. Si usas wallet proxy (Magic), ya están configurados.

---

### Paso 6: Primera ejecución (test de conexión)

**⏳ TAREA MANUAL**

```bash
cd polymarket-bot
source venv/bin/activate
python main.py
```

**Lo que deberías ver:**

```
12:00:01 │ polybot.main       │ INFO  │ ⚙️  Config cargada — simulation=True, chain=137
12:00:01 │ polybot.main       │ INFO  │ 🧪 MODO SIMULACIÓN activado
12:00:02 │ polybot.auth       │ INFO  │ ✅ Autenticación L1+L2 completada
12:00:03 │ polybot.discovery  │ INFO  │ 🔍 Iniciando escaneo de mercados...
12:00:04 │ polybot.discovery  │ INFO  │    📊 247 mercados activos obtenidos de Gamma
12:00:04 │ polybot.discovery  │ INFO  │    🎁 12 mercados con liquidity rewards
12:00:06 │ polybot.discovery  │ INFO  │    🔬 38 candidatos tras filtros duros
12:00:06 │ polybot.discovery  │ INFO  │ 🏆 Top 5 mercados seleccionados:
12:00:06 │ polybot.discovery  │ INFO  │    1. [78.3] BTC 5-min up or down — spread=1.2¢, rebates=✅
12:00:06 │ polybot.discovery  │ INFO  │    2. [71.5] ETH 15-min above $3800 — spread=1.8¢, rebates=✅
...
12:00:07 │ polybot.ws         │ INFO  │ ✅ Conectado a canal market
12:00:07 │ polybot.ws         │ INFO  │ 💓 Heartbeat OK
```

**Posibles errores y soluciones:**

| Error | Causa | Solución |
|-------|-------|----------|
| `PRIVATE_KEY no configurada` | Falta la clave en .env | Rellena el campo |
| `No se pudo conectar al CLOB` | Sin internet o endpoint incorrecto | Verifica conexión |
| `403 / Geographic Restriction` | IP geo-bloqueada | Usa VPS fuera de zonas restringidas |
| `Discovery no encontró mercados` | Filtros muy estrictos | Ajusta `min_volume_24h` o `max_spread_cents` en DiscoveryConfig |
| `Invalid signature` | Clave/signature_type incorrectos | Verifica que coincidan con tu wallet |

---

### Paso 7: Ajustar los filtros del Discovery (opcional)

**⏳ TAREA MANUAL — Solo si quieres afinar la selección**

Si el Discovery selecciona mercados que no te interesan o descarta los que sí, puedes ajustar los parámetros en `src/discovery.py`, clase `DiscoveryConfig`:

- `min_volume_24h`: Mínimo de volumen (bajar si ves pocos resultados)
- `max_spread_cents`: Máximo spread aceptable (subir si ves pocos resultados)
- `min_score_threshold`: Score mínimo para operar
- `max_active_markets`: Cuántos mercados operar a la vez
- `blacklist_conditions`: Lista de condition_ids a excluir siempre
- `whitelist_conditions`: Si no está vacío, SOLO opera estos

Los **pesos del scoring** también se pueden ajustar. Por ejemplo, si quieres priorizar más los maker rebates sobre el volumen, sube `w_maker_rebates` y baja `w_volume`.

---

### Paso 8: Preparar el VPS (para producción)

**⏳ TAREA MANUAL — Hacer antes de operar en vivo**

1. VPS recomendado: **DigitalOcean** o **Vultr**, 2 vCPU, 4 GB RAM, ubicación **US East (Nueva York)**.
2. Instalar Python 3 y dependencias.
3. Transferir proyecto (sin `.env` — escribirlo manualmente en el VPS).
4. Usar `tmux` o `systemd` para mantener el bot corriendo.

---

## Cómo funciona el Discovery (para que entiendas qué hace)

El módulo `src/discovery.py` ejecuta este pipeline cada 5 minutos:

1. **Consulta Gamma API** → obtiene todos los mercados activos
2. **Consulta endpoint de rewards** → identifica mercados con liquidity rewards
3. **Clasifica por categoría** → crypto corto plazo, deportes, finanzas, etc.
4. **Consulta fee-rate por token** → identifica mercados con taker fees (= maker rebates)
5. **Consulta orderbook** → obtiene best bid/ask y spread actual
6. **Aplica filtros duros** → descarta mercados con poco volumen, spread enorme, o casi decididos
7. **Calcula score** → fórmula ponderada con 10 factores
8. **Retorna ranking** → los N mejores mercados para market making

**Prioridad de mercados (por tu documento de estrategia):**
1. 🥇 Crypto corto plazo (BTC/ETH 5m-15m) — máxima prioridad
2. 🥈 Deportes y esports con incentivos
3. 🥉 Finanzas/economía/tech/weather con volumen
4. 🏅 Geopolítica/política — solo si muy líquido

**Factores del scoring (10 componentes ponderados):**

| Factor | Peso | Qué mide |
|--------|------|----------|
| Categoría | 15% | Prioridad según tipo de mercado |
| Maker rebates | 15% | ¿El mercado tiene taker fees → rebates? |
| Liquidity rewards | 12% | ¿Hay rewards activos + cuánto pagan? |
| Spread | 12% | Menor spread = mejor |
| Competencia | 12% | Menos makers compitiendo = mejor |
| Volumen 24h | 10% | Más volumen = más fills |
| Centralidad precio | 6% | Cercanía a $0.50 = más fees generadas |
| Frecuencia trades | 6% | Más actividad reciente = mejor |
| Tiempo resolución | 6% | Mercados con 1-48h = ideales |
| Riesgo evento | 6% | Crypto = bajo riesgo, política = alto |

**Supuestos sobre la API** (marcados con ⚠️ en el código para corrección rápida):
- La Gamma API devuelve tags para clasificar categorías
- El endpoint `/rewards/markets` tiene la info de liquidity rewards
- El fee-rate > 0 implica que el mercado tiene maker rebates
- Los mercados crypto se identifican por patrones en el nombre/slug

---

## Resumen de archivos generados en Fase 1

| Archivo | Descripción |
|---------|-------------|
| `.env.example` | Plantilla de configuración (copia como `.env`) |
| `config/settings.py` | Carga y valida configuración desde .env |
| `config/__init__.py` | Exporta funciones de config |
| `src/auth.py` | Autenticación L1 (firma) + L2 (API creds) |
| `src/websocket_manager.py` | Conexión WebSocket con heartbeat y reconexión |
| `src/orderbook.py` | Estado local del libro de órdenes |
| `src/discovery.py` | **NUEVO** — Selección automática de mercados con scoring |
| `main.py` | Punto de entrada que orquesta todo |
| `requirements.txt` | Dependencias Python |

---

## Próximas fases (pendientes)

| Fase | Prompt | Estado | Qué implementa |
|------|--------|--------|-----------------|
| 1 | Conexión, Auth + Discovery | ✅ COMPLETADA | Auth L1/L2, WebSocket, Heartbeat, Market Selector |
| 2 | Estrategia Maker + Velocidad | ✅ COMPLETADA | Cotización bidireccional, bucle cancel/replace, fee-rate dinámico |
| SSL | Bypass proxy corporativo | ✅ COMPLETADA | VERIFY_SSL para entornos con proxy y cert auto-firmado |
| 3 | Tarifas, Firmas y Re-scan | ✅ COMPLETADA | Enriquecimiento real de scoring, re-scan periódico |
| 4 | Gestión de Riesgo | ✅ COMPLETADA | Kill switch, Kelly fraccional, exposición total, P&L tracking |
| 5 | Monitorización | ⏳ PENDIENTE | Dashboard, alertas, auto-restart |

---

## Bugfixes aplicados sobre componentes de Fase 1

### Bug 1 — Deadlock en `OrderbookTracker` (commit `d850e02`)

**Causa raíz:** `process_book_event`, `process_price_change` y `process_last_trade` llamaban a `get_or_create()` dentro de un bloque `with self._lock:`. Como `threading.Lock` **no es reentrante**, el segundo intento de adquirir el lock provocaba un deadlock permanente que congelaba el event loop de asyncio: ningún `asyncio.sleep()` de otras corrutinas (trading loop, re-scan, heartbeat) podía dispararse jamás.

**Fix:** se eliminó la llamada a `get_or_create()` dentro del lock y se sustituyó por acceso directo al dict (`if token_id not in self._books: self._books[token_id] = ...`) dentro del mismo bloque, sin adquirir el lock por segunda vez.

**Síntoma observable:** el bot arrancaba (auth OK, discovery OK, WebSocket conectado) pero el trading loop nunca ejecutaba el Ciclo #100 ni los logs de riesgo.

### Bug 2 — Event loop starvation en `WebSocketManager` (commit `d850e02`)

**Causa raíz:** cuando el servidor de Polymarket envía ráfagas de mensajes, `asyncio.Queue.get()` no suspende si la cola tiene elementos — procesa mensaje tras mensaje sin ceder el control. Esto privaba de CPU a otras corrutinas (`asyncio.sleep()` del trading loop, re-scan, etc.).

**Fix:** se añade `await asyncio.sleep(0)` al final de `_handle_message` para forzar un yield al event loop tras cada mensaje, permitiendo que el scheduler de asyncio ejecute otras tareas pendientes.

**Síntoma observable:** el trading loop podía quedar bloqueado esperando tras una ráfaga inicial de eventos `book` del WebSocket.

---

*Última actualización: Fase 1 + Discovery + Bugfixes — Abril 2026*

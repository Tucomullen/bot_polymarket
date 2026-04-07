"""
src/dashboard.py — Dashboard web en tiempo real para el bot de Polymarket.

Tecnologia:
  - FastAPI: servidor HTTP asíncrono
  - SSE (Server-Sent Events): push de datos al navegador cada segundo
  - HTML + CSS inline: sin archivos estaticos externos

Endpoints:
  GET  /              -> dashboard HTML
  GET  /api/status    -> JSON con estado actual
  GET  /api/stream    -> SSE: actualizaciones en tiempo real (1/s)
  POST /api/kill-switch -> activa el kill switch manualmente
  GET  /api/logs      -> ultimas N lineas de log

Integracion:
  Crea un BotState y lo actualiza desde main.py pasando referencias
  al RiskManager y TradingLoop.

  from src.dashboard import DashboardServer, BotState
  state = BotState()
  dashboard = DashboardServer(state, port=8080)
  await dashboard.start()           # lanza uvicorn en background
  state.wire(risk_manager, trading_loop)
"""

import asyncio
import collections
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

if TYPE_CHECKING:
    from src.risk_manager import RiskManager
    from src.trading_loop import TradingLoop

logger = logging.getLogger("polybot.dashboard")

# ---------------------------------------------------------------------------
# BotState — estado compartido entre el bot y el dashboard
# ---------------------------------------------------------------------------

MAX_EVENTS = 50    # ultimas N ordenes/fills en el buffer
MAX_LOGS = 200     # ultimas N lineas de log en memoria


@dataclass
class MarketQuoteSnapshot:
    condition_id: str
    question: str
    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0
    spread_cents: float = 0.0


class BotState:
    """
    Estado centralizado del bot accesible desde el dashboard.

    El objeto vive en main.py y se pasa al DashboardServer. El trading loop
    lo actualiza via wire() + push_event(). El dashboard solo lee.
    """

    def __init__(self) -> None:
        self._risk: "RiskManager | None" = None
        self._loop: "TradingLoop | None" = None
        self._started_at: float = time.time()
        self._simulation: bool = True
        self._bankroll: float = 0.0
        self._kill_triggered_notified: bool = False

        # Buffers de eventos y logs (thread-safe con deque)
        self.events: collections.deque[dict] = collections.deque(maxlen=MAX_EVENTS)
        self.log_lines: collections.deque[str] = collections.deque(maxlen=MAX_LOGS)

        # Quotes activas por mercado
        self.market_quotes: dict[str, MarketQuoteSnapshot] = {}

    def wire(
        self,
        risk_manager: "RiskManager | None" = None,
        trading_loop: "TradingLoop | None" = None,
        simulation: bool = True,
        bankroll: float = 0.0,
    ) -> None:
        """Conecta el estado con los componentes del bot."""
        self._risk = risk_manager
        self._loop = trading_loop
        self._simulation = simulation
        self._bankroll = bankroll

    def push_event(self, event: dict) -> None:
        """Registra un evento de orden/fill en el buffer circular."""
        event.setdefault("ts", datetime.now(timezone.utc).strftime("%H:%M:%S"))
        self.events.appendleft(event)

    def push_log(self, line: str) -> None:
        """Registra una linea de log en el buffer circular."""
        self.log_lines.appendleft(line)

    def update_quote(self, condition_id: str, question: str,
                     bid: float, ask: float, mid: float) -> None:
        """Actualiza la quote activa de un mercado."""
        self.market_quotes[condition_id] = MarketQuoteSnapshot(
            condition_id=condition_id,
            question=question[:50],
            bid=bid,
            ask=ask,
            mid=mid,
            spread_cents=round((ask - bid) * 100, 2),
        )

    def snapshot(self) -> dict:
        """Devuelve un snapshot JSON del estado actual para el SSE stream."""
        risk_stats = self._risk.get_stats() if self._risk else {}
        loop_running = self._loop.is_running if self._loop else False
        cycle_count = self._loop.cycle_count if self._loop else 0
        order_metrics = self._loop.order_metrics if self._loop else {}

        uptime_s = time.time() - self._started_at
        uptime_str = _format_uptime(uptime_s)

        kill_active = risk_stats.get("kill_switch", False)

        return {
            "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "uptime": uptime_str,
            "simulation": self._simulation,
            "loop_running": loop_running,
            "cycle_count": cycle_count,
            "kill_switch": kill_active,
            "session_pnl": risk_stats.get("session_pnl_usd", 0.0),
            "exposure_usd": risk_stats.get("total_exposure_usd", 0.0),
            "exposure_pct": risk_stats.get("total_exposure_pct", 0.0),
            "consecutive_errors": risk_stats.get("consecutive_errors", 0),
            "orders_created": order_metrics.get("orders_created", 0),
            "orders_cancelled": order_metrics.get("orders_cancelled", 0),
            "fills": order_metrics.get("fills_received", 0),
            "markets": [
                {
                    "cid": q.condition_id,
                    "question": q.question,
                    "bid": q.bid,
                    "ask": q.ask,
                    "mid": q.mid,
                    "spread": q.spread_cents,
                }
                for q in self.market_quotes.values()
            ],
            "events": list(self.events)[:20],
        }


def _format_uptime(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


# ---------------------------------------------------------------------------
# Log handler — escribe JSON lines a disco + alimenta el buffer del dashboard
# ---------------------------------------------------------------------------

class DashboardLogHandler(logging.Handler):
    """
    Handler de logging que:
    1. Guarda el buffer en memoria para el endpoint /api/logs
    2. (Opcional) Escribe JSON lines a disco si se configura log_dir
    """

    def __init__(self, bot_state: BotState, log_dir: str = "") -> None:
        super().__init__()
        self._state = bot_state
        self._file = None
        if log_dir:
            import pathlib
            pathlib.Path(log_dir).mkdir(parents=True, exist_ok=True)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            log_path = pathlib.Path(log_dir) / f"bot_{today}.log"
            self._file = open(log_path, "a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            self._state.push_log(line)
            if self._file:
                json_line = json.dumps({
                    "ts": record.created,
                    "level": record.levelname,
                    "module": record.name,
                    "msg": record.getMessage(),
                })
                self._file.write(json_line + "\n")
                self._file.flush()
        except Exception:
            pass

    def close(self) -> None:
        super().close()
        if self._file:
            self._file.close()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

def create_app(state: BotState, password: str = "") -> FastAPI:
    """
    Crea la aplicacion FastAPI con todos los endpoints.

    Args:
        state: estado compartido del bot
        password: si se configura, el endpoint /api/kill-switch lo requiere
    """
    app = FastAPI(title="Polymarket Bot Dashboard", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------
    # GET / — Dashboard HTML
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(_HTML)

    # ------------------------------------------------------------------
    # GET /api/status — Snapshot JSON
    # ------------------------------------------------------------------

    @app.get("/api/status")
    async def status() -> JSONResponse:
        return JSONResponse(state.snapshot())

    # ------------------------------------------------------------------
    # GET /api/stream — SSE stream (1 evento/segundo)
    # ------------------------------------------------------------------

    @app.get("/api/stream")
    async def stream() -> StreamingResponse:
        async def event_generator():
            while True:
                data = json.dumps(state.snapshot())
                yield f"data: {data}\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ------------------------------------------------------------------
    # POST /api/kill-switch — Activa kill switch manualmente
    # ------------------------------------------------------------------

    @app.post("/api/kill-switch")
    async def kill_switch(body: dict = {}) -> JSONResponse:
        if password and body.get("password") != password:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        if state._risk is None:
            return JSONResponse({"error": "risk_manager not available"}, status_code=503)

        reason = body.get("reason", "activado manualmente desde el dashboard")
        state._risk._trigger(reason)
        logger.warning("Kill switch activado desde el dashboard: %s", reason)
        return JSONResponse({"ok": True, "reason": reason})

    # ------------------------------------------------------------------
    # GET /api/logs — Ultimas N lineas de log
    # ------------------------------------------------------------------

    @app.get("/api/logs")
    async def logs(n: int = 100) -> JSONResponse:
        lines = list(state.log_lines)[:n]
        return JSONResponse({"lines": lines, "total": len(state.log_lines)})

    return app


# ---------------------------------------------------------------------------
# DashboardServer — wrapper para arrancar uvicorn como tarea asyncio
# ---------------------------------------------------------------------------

class DashboardServer:
    """
    Arranca el dashboard FastAPI en background como tarea asyncio.

    Uso:
        server = DashboardServer(state, port=8080)
        task = asyncio.create_task(server.start())
    """

    def __init__(
        self,
        state: BotState,
        port: int = 8080,
        host: str = "0.0.0.0",
        password: str = "",
    ) -> None:
        self._state = state
        self._port = port
        self._host = host
        self._app = create_app(state, password)

    async def start(self) -> None:
        """Arranca uvicorn en el event loop actual."""
        import uvicorn
        config = uvicorn.Config(
            app=self._app,
            host=self._host,
            port=self._port,
            log_level="warning",   # uvicorn silencioso para no saturar logs del bot
            access_log=False,
        )
        server = uvicorn.Server(config)
        logger.info("Dashboard disponible en http://%s:%d", self._host, self._port)
        await server.serve()


# ---------------------------------------------------------------------------
# HTML del dashboard (inline)
# ---------------------------------------------------------------------------

_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket Bot</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0f172a;color:#e2e8f0;padding:20px;font-size:14px}
h1{font-size:1.3rem;color:#f8fafc;margin-bottom:2px}
.subtitle{color:#475569;font-size:0.8rem;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:20px}
.card{background:#1e293b;border-radius:8px;padding:14px}
.card-label{font-size:0.7rem;color:#64748b;text-transform:uppercase;letter-spacing:.05em}
.card-value{font-size:1.4rem;font-weight:700;margin-top:4px}
.green{color:#22c55e} .red{color:#ef4444} .yellow{color:#eab308} .blue{color:#3b82f6} .gray{color:#64748b}
.status-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.dot-green{background:#22c55e;box-shadow:0 0 6px #22c55e}
.dot-red{background:#ef4444;box-shadow:0 0 6px #ef4444}
.dot-gray{background:#475569}
.section{font-size:.75rem;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.05em;margin:16px 0 8px;border-bottom:1px solid #1e293b;padding-bottom:4px}
table{width:100%;border-collapse:collapse;font-size:.82rem}
th{background:#1e293b;color:#64748b;text-align:left;padding:6px 10px;font-weight:500}
td{padding:6px 10px;border-bottom:1px solid #0f172a}
.pill{display:inline-block;padding:1px 7px;border-radius:4px;font-size:.7rem;font-weight:600}
.pill-buy{background:#1d4ed8;color:#bfdbfe}
.pill-sell{background:#166534;color:#bbf7d0}
.pill-sim{background:#374151;color:#9ca3af}
.kill-btn{background:#dc2626;color:white;border:none;padding:8px 16px;border-radius:6px;cursor:pointer;font-size:.82rem;font-weight:600;float:right}
.kill-btn:hover{background:#b91c1c}
.kill-btn:disabled{background:#374151;color:#6b7280;cursor:not-allowed}
.logs{background:#1e293b;border-radius:8px;padding:12px;font-family:monospace;font-size:.72rem;color:#94a3b8;max-height:240px;overflow-y:auto;line-height:1.5}
.log-error{color:#ef4444} .log-warn{color:#eab308} .log-info{color:#94a3b8}
.kill-banner{background:#7f1d1d;border:1px solid #ef4444;border-radius:8px;padding:12px 16px;margin-bottom:16px;color:#fca5a5;font-weight:600;display:none}
</style>
</head>
<body>
<h1>
  <span class="status-dot" id="dot"></span>
  POLYMARKET BOT
  <button class="kill-btn" id="killBtn" onclick="activateKillSwitch()">KILL SWITCH</button>
</h1>
<div class="subtitle" id="subtitle">Conectando...</div>

<div id="killBanner" class="kill-banner">KILL SWITCH ACTIVO — Todas las ordenes canceladas. Revision manual requerida.</div>

<div class="grid">
  <div class="card"><div class="card-label">Uptime</div><div class="card-value" id="uptime">—</div></div>
  <div class="card"><div class="card-label">P&L Sesion</div><div class="card-value" id="pnl">—</div></div>
  <div class="card"><div class="card-label">Exposicion</div><div class="card-value" id="exposure">—</div></div>
  <div class="card"><div class="card-label">Ciclos</div><div class="card-value blue" id="cycles">—</div></div>
  <div class="card"><div class="card-label">Fills</div><div class="card-value blue" id="fills">—</div></div>
  <div class="card"><div class="card-label">Errores</div><div class="card-value" id="errors">—</div></div>
</div>

<div class="section">Mercados activos</div>
<table id="marketsTable">
  <thead><tr><th>Mercado</th><th>Bid</th><th>Ask</th><th>Spread</th><th>Mid</th></tr></thead>
  <tbody id="marketsBody"><tr><td colspan="5" style="color:#475569">Esperando datos...</td></tr></tbody>
</table>

<div class="section">Actividad reciente</div>
<table id="eventsTable">
  <thead><tr><th>Hora</th><th>Tipo</th><th>Lado</th><th>Precio</th><th>Mercado</th></tr></thead>
  <tbody id="eventsBody"><tr><td colspan="5" style="color:#475569">Sin actividad</td></tr></tbody>
</table>

<div class="section">Logs recientes</div>
<div class="logs" id="logsDiv">Esperando logs...</div>

<script>
const $ = id => document.getElementById(id);

function pnlColor(v) {
  if (v > 0) return 'green';
  if (v < 0) return 'red';
  return 'gray';
}

function update(d) {
  // Status dot
  const dot = $('dot');
  if (d.kill_switch) {
    dot.className = 'status-dot dot-red';
    $('killBanner').style.display = 'block';
    $('killBtn').disabled = true;
  } else if (d.loop_running) {
    dot.className = 'status-dot dot-green';
    $('killBanner').style.display = 'none';
    $('killBtn').disabled = false;
  } else {
    dot.className = 'status-dot dot-gray';
  }

  const mode = d.simulation ? '[SIM]' : '[LIVE]';
  $('subtitle').textContent = `${mode} — ${d.ts}`;
  $('uptime').textContent = d.uptime;

  const pnl = d.session_pnl || 0;
  const pnlEl = $('pnl');
  pnlEl.textContent = (pnl >= 0 ? '+' : '') + pnl.toFixed(4) + ' $';
  pnlEl.className = 'card-value ' + pnlColor(pnl);

  $('exposure').textContent = (d.exposure_pct || 0).toFixed(1) + '% ($' + (d.exposure_usd || 0).toFixed(0) + ')';
  $('cycles').textContent = (d.cycle_count || 0).toLocaleString();
  $('fills').textContent = (d.fills || 0);

  const errEl = $('errors');
  errEl.textContent = d.consecutive_errors || 0;
  errEl.className = 'card-value ' + (d.consecutive_errors > 0 ? 'red' : 'green');

  // Markets table
  const mb = $('marketsBody');
  if (d.markets && d.markets.length > 0) {
    mb.innerHTML = d.markets.map(m =>
      `<tr>
        <td title="${m.cid}">${m.question}</td>
        <td class="green">${m.bid.toFixed(3)}</td>
        <td class="red">${m.ask.toFixed(3)}</td>
        <td class="yellow">${m.spread.toFixed(1)}c</td>
        <td>${m.mid.toFixed(3)}</td>
      </tr>`
    ).join('');
  } else {
    mb.innerHTML = '<tr><td colspan="5" style="color:#475569">Sin mercados activos</td></tr>';
  }

  // Events table
  const eb = $('eventsBody');
  if (d.events && d.events.length > 0) {
    eb.innerHTML = d.events.slice(0, 20).map(e =>
      `<tr>
        <td class="gray">${e.ts || ''}</td>
        <td><span class="pill pill-${(e.type||'').toLowerCase()}">${e.type || '?'}</span>${e.simulation ? ' <span class="pill pill-sim">SIM</span>' : ''}</td>
        <td class="${e.side === 'BUY' ? 'blue' : 'green'}">${e.side || ''}</td>
        <td>${e.price ? parseFloat(e.price).toFixed(3) : ''}</td>
        <td style="color:#64748b" title="${e.condition_id || ''}">${(e.question || e.condition_id || '').substring(0,30)}</td>
      </tr>`
    ).join('');
  } else {
    eb.innerHTML = '<tr><td colspan="5" style="color:#475569">Sin actividad</td></tr>';
  }
}

function updateLogs(lines) {
  const el = $('logsDiv');
  el.innerHTML = lines.slice(0, 60).map(l => {
    let cls = 'log-info';
    if (l.includes('ERROR') || l.includes('CRITICAL') || l.includes('💥')) cls = 'log-error';
    else if (l.includes('WARNING') || l.includes('WARN') || l.includes('⚠')) cls = 'log-warn';
    return `<div class="${cls}">${escHtml(l)}</div>`;
  }).join('');
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

async function activateKillSwitch() {
  if (!confirm('Activar KILL SWITCH? Esto cancelara todas las ordenes abiertas.')) return;
  try {
    const r = await fetch('/api/kill-switch', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({reason:'activado manualmente'})});
    const d = await r.json();
    if (d.ok) alert('Kill switch activado correctamente.');
    else alert('Error: ' + JSON.stringify(d));
  } catch(e) { alert('Error de red: ' + e); }
}

// SSE stream
function connectSSE() {
  const es = new EventSource('/api/stream');
  es.onmessage = e => {
    try { update(JSON.parse(e.data)); } catch(_) {}
  };
  es.onerror = () => {
    $('dot').className = 'status-dot dot-gray';
    $('subtitle').textContent = 'Reconectando...';
    setTimeout(connectSSE, 3000);
    es.close();
  };
}

// Logs: fetch cada 3 segundos
async function fetchLogs() {
  try {
    const r = await fetch('/api/logs?n=60');
    const d = await r.json();
    updateLogs(d.lines || []);
  } catch(_) {}
}

connectSSE();
fetchLogs();
setInterval(fetchLogs, 3000);
</script>
</body>
</html>"""

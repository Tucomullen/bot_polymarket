"""
backtesting/report.py — Genera un informe HTML con gráficas del backtest.

Genera un único archivo HTML auto-contenido con:
  - Tabla resumen por mercado
  - Gráfica de P&L acumulado
  - Gráfica de precio histórico + nuestros bids/asks
  - Distribución de fills
"""

import logging
import math
from datetime import datetime, timezone
from pathlib import Path

from backtesting.simulator import BacktestResult, SimStep, FillEvent
from backtesting.metrics import BacktestMetrics, aggregate_metrics

logger = logging.getLogger("polybot.backtest.report")


# ---------------------------------------------------------------------------
# Generación de SVG inline (sin matplotlib como dependencia dura)
# ---------------------------------------------------------------------------

def _sparkline_svg(values: list[float], width: int = 200, height: int = 40,
                   color: str = "#22c55e") -> str:
    """Genera un sparkline SVG simple para una serie de valores."""
    if len(values) < 2:
        return f'<svg width="{width}" height="{height}"></svg>'

    mn = min(values)
    mx = max(values)
    rng = mx - mn if mx != mn else 1.0

    def scale_y(v: float) -> float:
        return height - 4 - ((v - mn) / rng) * (height - 8)

    n = len(values)
    pts = " ".join(
        f"{round(i / (n - 1) * width, 1)},{round(scale_y(v), 1)}"
        for i, v in enumerate(values)
    )
    return (
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.5"/>'
        f'</svg>'
    )


def _pnl_chart_svg(steps: list[SimStep], fills: list[FillEvent],
                   width: int = 700, height: int = 200) -> str:
    """Genera un SVG del P&L acumulado con marcadores de fills."""
    if len(steps) < 2:
        return f'<svg width="{width}" height="{height}"><text x="10" y="20" fill="#888">Sin datos</text></svg>'

    pnl_values = [s.cumulative_pnl for s in steps]
    ts_values = [s.timestamp for s in steps]

    mn = min(pnl_values)
    mx = max(pnl_values)
    rng = mx - mn if mx != mn else 1.0
    ts_min = ts_values[0]
    ts_rng = ts_values[-1] - ts_values[0] if ts_values[-1] != ts_values[0] else 1.0

    pad = 30
    inner_w = width - pad * 2
    inner_h = height - pad * 2

    def sx(ts: float) -> float:
        return pad + (ts - ts_min) / ts_rng * inner_w

    def sy(v: float) -> float:
        return pad + inner_h - ((v - mn) / rng) * inner_h

    # Línea de P&L
    points = " ".join(f"{sx(s.timestamp):.1f},{sy(s.cumulative_pnl):.1f}" for s in steps)

    # Línea del cero
    zero_y = sy(0.0)
    zero_line = (
        f'<line x1="{pad}" y1="{zero_y:.1f}" x2="{pad + inner_w}" y2="{zero_y:.1f}" '
        f'stroke="#475569" stroke-width="0.5" stroke-dasharray="4"/>'
    )

    # Fills
    fill_marks = []
    for f in fills:
        if f.timestamp < ts_min:
            continue
        fx = sx(f.timestamp)
        step_pnl = next((s.cumulative_pnl for s in steps if s.timestamp >= f.timestamp), pnl_values[-1])
        fy = sy(step_pnl)
        color = "#22c55e" if f.side == "SELL" else "#3b82f6"
        fill_marks.append(f'<circle cx="{fx:.1f}" cy="{fy:.1f}" r="3" fill="{color}" opacity="0.7"/>')

    # Etiquetas de eje Y
    labels = []
    for v in [mn, (mn + mx) / 2, mx]:
        y = sy(v)
        labels.append(f'<text x="{pad - 4}" y="{y:.1f}" text-anchor="end" '
                       f'font-size="9" fill="#94a3b8">{v:+.2f}</text>')

    pnl_color = "#22c55e" if pnl_values[-1] >= 0 else "#ef4444"
    svg = (
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" '
        f'style="background:#1e293b;border-radius:6px">'
        f'{zero_line}'
        f'<polyline points="{points}" fill="none" stroke="{pnl_color}" stroke-width="1.5"/>'
        + "".join(fill_marks)
        + "".join(labels)
        + f'<text x="{pad}" y="{pad - 6}" font-size="10" fill="#94a3b8">P&L Acumulado ($)</text>'
        f'</svg>'
    )
    return svg


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0f172a; color: #e2e8f0; padding: 24px; }
h1 { font-size: 1.5rem; margin-bottom: 4px; color: #f8fafc; }
.subtitle { color: #64748b; font-size: 0.85rem; margin-bottom: 24px; }
.summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 28px; }
.card { background: #1e293b; border-radius: 8px; padding: 16px; }
.card-label { font-size: 0.75rem; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; }
.card-value { font-size: 1.5rem; font-weight: 700; margin-top: 4px; }
.green { color: #22c55e; } .red { color: #ef4444; } .blue { color: #3b82f6; } .yellow { color: #eab308; }
.section-title { font-size: 1rem; font-weight: 600; margin: 24px 0 12px; color: #cbd5e1; border-bottom: 1px solid #1e293b; padding-bottom: 6px; }
table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
th { background: #1e293b; color: #64748b; text-align: left; padding: 8px 10px; font-weight: 500; }
td { padding: 7px 10px; border-bottom: 1px solid #1e293b; vertical-align: middle; }
tr:hover td { background: #1e293b; }
.pass { color: #22c55e; font-weight: 600; }
.fail { color: #ef4444; font-weight: 600; }
.chart-wrap { background: #1e293b; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
.market-title { font-size: 0.85rem; color: #94a3b8; margin-bottom: 8px; }
.reasons { font-size: 0.75rem; color: #ef4444; margin-top: 4px; }
"""

def _verdict_card(agg: dict) -> str:
    verdict = agg.get("verdict", "FAIL")
    color = "green" if verdict == "PASS" else "red"
    return f'<div class="card"><div class="card-label">Veredicto</div><div class="card-value {color}">{verdict}</div></div>'


def generate_report(
    results: list[BacktestResult],
    metrics: list[BacktestMetrics],
    bankroll: float,
    days: int,
    output_path: Path | None = None,
) -> Path:
    """
    Genera el informe HTML auto-contenido.

    Returns:
        Path al archivo HTML generado.
    """
    if output_path is None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
        output_path = Path(__file__).parent / f"report_{ts}.html"

    agg = aggregate_metrics(metrics)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ---- Summary cards ----
    total_pnl = agg.get("total_pnl_usd", 0.0)
    pnl_pct = total_pnl / max(bankroll, 1) * 100
    pnl_color = "green" if total_pnl >= 0 else "red"

    cards_html = f"""
    <div class="summary-grid">
      <div class="card"><div class="card-label">P&L Total</div>
        <div class="card-value {pnl_color}">${total_pnl:+.2f}</div></div>
      <div class="card"><div class="card-label">Retorno</div>
        <div class="card-value {pnl_color}">{pnl_pct:+.2f}%</div></div>
      <div class="card"><div class="card-label">Sharpe Medio</div>
        <div class="card-value blue">{agg.get('avg_sharpe', 0):.2f}</div></div>
      <div class="card"><div class="card-label">Max Drawdown</div>
        <div class="card-value yellow">{agg.get('worst_drawdown_pct', 0):.1f}%</div></div>
      <div class="card"><div class="card-label">Fill Rate Medio</div>
        <div class="card-value blue">{agg.get('avg_fill_rate_pct', 0):.1f}%</div></div>
      <div class="card"><div class="card-label">Mercados OK</div>
        <div class="card-value">{agg.get('markets_passing', 0)}/{agg.get('n_markets', 0)}</div></div>
      {_verdict_card(agg)}
    </div>
    """

    # ---- Metrics table ----
    rows = []
    for m in metrics:
        status = '<span class="pass">✅ PASA</span>' if m.passes else '<span class="fail">❌ FALLA</span>'
        pnl_cls = "green" if m.pnl_usd >= 0 else "red"
        reasons = ""
        if m.failure_reasons:
            reasons = '<div class="reasons">' + " | ".join(m.failure_reasons) + "</div>"
        row = f"""
        <tr>
          <td>{m.question[:45]}{reasons}</td>
          <td class="{pnl_cls}">${m.pnl_usd:+.4f}</td>
          <td class="{pnl_cls}">{m.pnl_pct:+.2f}%</td>
          <td>{m.sharpe_ratio:.2f}</td>
          <td>{m.max_drawdown_pct:.1f}%</td>
          <td>{m.fill_rate_pct:.1f}%</td>
          <td>{m.profitable_days_pct:.0f}%</td>
          <td>{m.n_days:.0f}d</td>
          <td>{status}</td>
        </tr>"""
        rows.append(row)

    table_html = f"""
    <div class="section-title">Resultados por mercado</div>
    <table>
      <thead><tr>
        <th>Mercado</th><th>P&L ($)</th><th>Retorno</th><th>Sharpe</th>
        <th>Max DD</th><th>Fill Rate</th><th>Días OK</th><th>Período</th><th>Estado</th>
      </tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    """

    # ---- Charts ----
    charts_html = '<div class="section-title">P&L Acumulado por mercado</div>'
    result_map = {r.condition_id: r for r in results}
    for m in metrics:
        r = result_map.get(m.condition_id)
        if not r or not r.steps:
            continue
        chart = _pnl_chart_svg(r.steps, r.fills)
        charts_html += f"""
        <div class="chart-wrap">
          <div class="market-title">{m.question[:60]}</div>
          {chart}
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Backtest — Polymarket Bot</title>
<style>{_CSS}</style>
</head>
<body>
<h1>Backtest — Polymarket Bot</h1>
<div class="subtitle">Generado: {now} | Período: {days} días | Bankroll: ${bankroll:.0f} | Mercados: {len(metrics)}</div>
{cards_html}
{table_html}
{charts_html}
</body>
</html>"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    logger.info("📄 Informe guardado en: %s", output_path)
    return output_path

"""
backtesting/metrics.py — Cálculo de métricas de rendimiento del backtest.

Métricas calculadas:
  - P&L neto y % retorno
  - Sharpe ratio (diario, anualizado)
  - Max drawdown absoluto y %
  - Fill rate (% de steps con al menos un fill)
  - Días rentables (% de días con P&L positivo)
  - Profit factor (ganancias brutas / pérdidas brutas)
"""

import math
import logging
from dataclasses import dataclass, field

from backtesting.simulator import BacktestResult, SimStep

logger = logging.getLogger("polybot.backtest.metrics")

# ---------------------------------------------------------------------------
# Umbrales mínimos (del PLAN_PREPRODUCCION.md)
# ---------------------------------------------------------------------------

MIN_PNL = 0.0
MIN_SHARPE = 0.5
MAX_DRAWDOWN_PCT = 15.0
MIN_FILL_RATE = 5.0
MIN_PROFITABLE_DAYS = 60.0


@dataclass
class BacktestMetrics:
    """Métricas de rendimiento calculadas sobre un BacktestResult."""
    condition_id: str
    question: str

    pnl_usd: float = 0.0
    pnl_pct: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_usd: float = 0.0
    max_drawdown_pct: float = 0.0
    fill_rate_pct: float = 0.0
    n_fills: int = 0
    total_volume_usd: float = 0.0
    n_days: float = 0.0
    profitable_days_pct: float = 0.0
    profit_factor: float = 0.0
    avg_pnl_per_fill: float = 0.0
    passes: bool = False
    failure_reasons: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        status = "✅ PASA" if self.passes else "❌ FALLA"
        return (
            f"{status} | {self.question[:35]} | "
            f"PnL={self.pnl_usd:+.2f}$ ({self.pnl_pct:+.1f}%) | "
            f"Sharpe={self.sharpe_ratio:.2f} | "
            f"DD={self.max_drawdown_pct:.1f}% | "
            f"FillRate={self.fill_rate_pct:.1f}% | "
            f"DíasRentables={self.profitable_days_pct:.0f}%"
        )


def _daily_pnl(steps: list[SimStep]) -> list[float]:
    """Agrupa los P&L por día."""
    if not steps:
        return []
    daily: dict[int, float] = {}
    prev_pnl = 0.0
    for s in steps:
        day = int(s.timestamp // 86400)
        delta = s.cumulative_pnl - prev_pnl
        daily[day] = daily.get(day, 0.0) + delta
        prev_pnl = s.cumulative_pnl
    return list(daily.values())


def _sharpe(daily_returns: list[float], risk_free_daily: float = 0.0) -> float:
    """Calcula el Sharpe ratio anualizado a partir de retornos diarios."""
    n = len(daily_returns)
    if n < 2:
        return 0.0
    mean = sum(daily_returns) / n
    variance = sum((r - mean) ** 2 for r in daily_returns) / (n - 1)
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std == 0:
        return 0.0
    return (mean - risk_free_daily) / std * math.sqrt(365)


def calculate_metrics(result: BacktestResult) -> BacktestMetrics:
    """Calcula todas las métricas de rendimiento para un BacktestResult."""
    m = BacktestMetrics(
        condition_id=result.condition_id,
        question=result.question,
        pnl_usd=round(result.final_pnl, 4),
        pnl_pct=round(result.return_pct, 2),
        max_drawdown_usd=round(result.max_drawdown, 4),
        max_drawdown_pct=round(result.max_drawdown_pct, 2),
        fill_rate_pct=round(result.fill_rate, 2),
        n_fills=result.n_fills,
        total_volume_usd=round(result.total_volume_usd, 2),
    )

    if result.steps:
        m.n_days = round((result.steps[-1].timestamp - result.steps[0].timestamp) / 86400, 1)

    daily = _daily_pnl(result.steps)
    m.sharpe_ratio = round(_sharpe(daily), 3)

    if daily:
        profitable = sum(1 for d in daily if d > 0)
        m.profitable_days_pct = round(profitable / len(daily) * 100, 1)

    gross_gains = sum(f.pnl_this_fill for f in result.fills if f.pnl_this_fill > 0)
    gross_losses = abs(sum(f.pnl_this_fill for f in result.fills if f.pnl_this_fill < 0))
    m.profit_factor = round(gross_gains / gross_losses, 3) if gross_losses > 0 else (
        float("inf") if gross_gains > 0 else 0.0
    )
    m.avg_pnl_per_fill = round(result.final_pnl / max(result.n_fills, 1), 6)

    reasons = []
    if m.pnl_usd <= MIN_PNL:
        reasons.append(f"PnL={m.pnl_usd:.4f} <= 0")
    if m.sharpe_ratio < MIN_SHARPE:
        reasons.append(f"Sharpe={m.sharpe_ratio:.2f} < {MIN_SHARPE}")
    if m.max_drawdown_pct > MAX_DRAWDOWN_PCT:
        reasons.append(f"Drawdown={m.max_drawdown_pct:.1f}% > {MAX_DRAWDOWN_PCT}%")
    if m.fill_rate_pct < MIN_FILL_RATE:
        reasons.append(f"FillRate={m.fill_rate_pct:.1f}% < {MIN_FILL_RATE}%")
    if m.profitable_days_pct < MIN_PROFITABLE_DAYS and m.n_days >= 7:
        reasons.append(f"DiasRentables={m.profitable_days_pct:.0f}% < {MIN_PROFITABLE_DAYS}%")

    m.failure_reasons = reasons
    m.passes = len(reasons) == 0
    return m


def aggregate_metrics(all_metrics: list[BacktestMetrics]) -> dict:
    """Agrega métricas de todos los mercados en un resumen global."""
    if not all_metrics:
        return {}
    total_pnl = sum(m.pnl_usd for m in all_metrics)
    avg_sharpe = sum(m.sharpe_ratio for m in all_metrics) / len(all_metrics)
    worst_dd = max(m.max_drawdown_pct for m in all_metrics)
    avg_fill_rate = sum(m.fill_rate_pct for m in all_metrics) / len(all_metrics)
    markets_passing = sum(1 for m in all_metrics if m.passes)
    return {
        "n_markets": len(all_metrics),
        "markets_passing": markets_passing,
        "total_pnl_usd": round(total_pnl, 4),
        "avg_sharpe": round(avg_sharpe, 3),
        "worst_drawdown_pct": round(worst_dd, 2),
        "avg_fill_rate_pct": round(avg_fill_rate, 2),
        "verdict": "PASS" if markets_passing >= len(all_metrics) * 0.6 else "FAIL",
    }


def print_summary(all_metrics: list[BacktestMetrics], bankroll: float, days: int) -> None:
    """Imprime el resumen del backtest en la consola."""
    agg = aggregate_metrics(all_metrics)
    print()
    print("=" * 70)
    print(f"  BACKTEST — {days} días | Bankroll ${bankroll:.0f}")
    print("=" * 70)
    for m in all_metrics:
        print(f"  {m.summary_line()}")
        if m.failure_reasons:
            for r in m.failure_reasons:
                print(f"    ↳ {r}")
    print("-" * 70)
    print(f"  TOTAL  PnL neto: ${agg.get('total_pnl_usd', 0):+.2f} | "
          f"Sharpe medio: {agg.get('avg_sharpe', 0):.2f} | "
          f"DD máx: {agg.get('worst_drawdown_pct', 0):.1f}% | "
          f"Fill rate: {agg.get('avg_fill_rate_pct', 0):.1f}%")
    verdict = agg.get("verdict", "FAIL")
    emoji = "✅" if verdict == "PASS" else "❌"
    print(f"  {emoji} VEREDICTO: {verdict} "
          f"({agg.get('markets_passing', 0)}/{agg.get('n_markets', 0)} mercados superan umbrales)")
    print("=" * 70)
    print()

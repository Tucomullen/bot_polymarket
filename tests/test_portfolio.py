"""tests/test_portfolio.py — Tests unitarios del PortfolioAllocator."""

import pytest
from src.portfolio import PortfolioAllocator, STRATEGY_MM, STRATEGY_ARB
from src.risk_manager import RiskManager


def _make_rm(bankroll: float = 1000.0) -> RiskManager:
    return RiskManager(
        bankroll_usd=bankroll,
        kelly_fraction=0.25,
        max_order_risk_pct=0.05,
        max_total_exposure_pct=0.20,
        max_session_loss_pct=0.03,
        max_consecutive_errors=10,
    )


def _make_alloc(bankroll: float = 1000.0, allocations: dict | None = None) -> PortfolioAllocator:
    allocs = allocations or {STRATEGY_MM: 0.60, STRATEGY_ARB: 0.40}
    return PortfolioAllocator(bankroll=bankroll, allocations=allocs, risk_manager=_make_rm(bankroll))


# ── Test 1: Presupuestos asignados correctamente ─────────────────────────────

def test_budget_allocation():
    alloc = _make_alloc(bankroll=1000.0, allocations={STRATEGY_MM: 0.40, STRATEGY_ARB: 0.60})
    assert alloc.get_budget(STRATEGY_MM) == 400.0
    assert alloc.get_budget(STRATEGY_ARB) == 600.0
    assert alloc.get_budget("inexistente") == 0.0


# ── Test 2: Allocations que no suman 1.0 lanzan ValueError ───────────────────

def test_allocations_must_sum_to_one():
    rm = _make_rm()
    with pytest.raises(ValueError, match="1.0"):
        PortfolioAllocator(
            bankroll=1000.0,
            allocations={STRATEGY_MM: 0.40, STRATEGY_ARB: 0.40},  # suma 0.80
            risk_manager=rm,
        )


# ── Test 3: get_available se reduce al abrir una orden ───────────────────────

def test_available_decreases_on_order_opened():
    alloc = _make_alloc(bankroll=1000.0, allocations={STRATEGY_MM: 1.0})
    assert alloc.get_available(STRATEGY_MM) == 1000.0

    alloc.record_order_opened(STRATEGY_MM, "cid-abc", 150.0)
    assert alloc.get_available(STRATEGY_MM) == 850.0

    alloc.record_order_closed(STRATEGY_MM, "cid-abc", 150.0)
    assert alloc.get_available(STRATEGY_MM) == 1000.0


# ── Test 4: Kill switch usa bankroll total, no el budget de la estrategia ────

def test_kill_switch_uses_total_bankroll():
    # Bankroll $1000, kill switch al 3% = $30 de pérdida
    # MM tiene solo $400, pero la pérdida que dispara el kill switch es $30 del total
    rm = _make_rm(bankroll=1000.0)
    alloc = PortfolioAllocator(
        bankroll=1000.0,
        allocations={STRATEGY_MM: 0.40, STRATEGY_ARB: 0.60},
        risk_manager=rm,
    )

    # Simular pérdida de $25 (< 3% de $1000, NO debe disparar KS)
    # Lo hacemos via record_fill: compramos caro y vendemos barato
    alloc.record_fill(STRATEGY_MM, "cid-x", "BUY", 0.50, 50.0)   # pagamos $25
    alloc.record_fill(STRATEGY_MM, "cid-x", "SELL", 0.45, 50.0)  # recibimos $22.50
    triggered, _ = rm.check_kill_switch()
    assert not triggered  # pérdida $2.50, por debajo del límite de $30

    # Simular pérdida adicional que supera $30 total
    alloc.record_fill(STRATEGY_ARB, "cid-y", "BUY", 0.50, 100.0)  # pagamos $50
    alloc.record_fill(STRATEGY_ARB, "cid-y", "SELL", 0.20, 100.0) # recibimos $20 → pérdida $30
    triggered, reason = rm.check_kill_switch()
    assert triggered
    assert "pérdida" in reason


# ── Test 5: Kelly sizing usa el budget, no el bankroll total ─────────────────

def test_kelly_uses_budget_not_total_bankroll():
    rm = _make_rm(bankroll=1000.0)

    # Con bankroll total: kelly_size = 1000 * 0.25 * edge * 2
    # Con budget $400 (40%): kelly_size = 400 * 0.25 * edge * 2 → debe ser menor
    size_total = rm.max_order_size_usd("cid", half_spread_cents=2.0, mid_price=0.50)
    size_budget = rm.max_order_size_usd("cid", half_spread_cents=2.0, mid_price=0.50,
                                        budget_override=400.0)

    assert size_budget < size_total
    # Proporcional: budget es 40% del bankroll → size también ~40%
    assert abs(size_budget / size_total - 0.40) < 0.01

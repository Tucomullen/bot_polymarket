"""
tests/test_risk_manager.py — Tests del RiskManager.

Cubre: kill switch (pérdida y errores), Kelly sizing, tracking de exposición,
registro de fills y cálculo de P&L.
"""

import pytest
from src.risk_manager import RiskManager


# ---------------------------------------------------------------------------
# Kill switch — pérdida de sesión
# ---------------------------------------------------------------------------

class TestKillSwitchLoss:

    def test_not_triggered_initially(self, risk):
        triggered, _ = risk.check_kill_switch()
        assert not triggered

    def test_triggers_on_session_loss(self, risk):
        """Se activa si la pérdida supera max_session_loss_pct * bankroll."""
        # Bankroll=$1000, max_loss=5% → umbral=$50
        risk._session_pnl = -51.0
        triggered, reason = risk.check_kill_switch()
        assert triggered
        assert "pérdida" in reason

    def test_does_not_trigger_just_below_threshold(self, risk):
        """Justo por debajo del umbral no debe activarse."""
        risk._session_pnl = -49.99
        triggered, _ = risk.check_kill_switch()
        assert not triggered

    def test_stays_triggered_after_reset_without_call(self, risk):
        """Una vez activado, sigue activo en llamadas posteriores."""
        risk._session_pnl = -100.0
        risk.check_kill_switch()
        risk._session_pnl = 0.0   # recuperamos el P&L (no debería importar)
        triggered, _ = risk.check_kill_switch()
        assert triggered

    def test_reset_clears_kill_switch(self, risk):
        """reset_kill_switch permite reanudar operaciones."""
        risk._session_pnl = -100.0
        risk.check_kill_switch()
        risk.reset_kill_switch()
        risk._session_pnl = 0.0  # también limpiar la pérdida para evitar re-trigger
        triggered, _ = risk.check_kill_switch()
        assert not triggered


# ---------------------------------------------------------------------------
# Kill switch — errores consecutivos
# ---------------------------------------------------------------------------

class TestKillSwitchErrors:

    def test_triggers_on_max_errors(self, risk):
        """Se activa tras max_consecutive_errors errores seguidos."""
        for _ in range(5):          # fixture: max_consecutive_errors=5
            risk.record_error()
        triggered, reason = risk.check_kill_switch()
        assert triggered
        assert "errores" in reason

    def test_success_resets_error_count(self, risk):
        """Un éxito reinicia el contador de errores."""
        for _ in range(4):
            risk.record_error()
        risk.record_success()
        for _ in range(4):
            risk.record_error()
        triggered, _ = risk.check_kill_switch()
        assert not triggered   # solo 4 errores tras el reset, umbral=5

    def test_does_not_trigger_below_threshold(self, risk):
        for _ in range(4):
            risk.record_error()
        triggered, _ = risk.check_kill_switch()
        assert not triggered


# ---------------------------------------------------------------------------
# Exposición
# ---------------------------------------------------------------------------

class TestExposure:

    def test_record_order_opened_increases_exposure(self, risk):
        risk.record_order_opened("0xmkt1", 50.0)
        assert risk.get_market_exposure("0xmkt1") == pytest.approx(50.0)
        assert risk.get_total_exposure() == pytest.approx(50.0)

    def test_record_order_closed_decreases_exposure(self, risk):
        risk.record_order_opened("0xmkt1", 50.0)
        risk.record_order_closed("0xmkt1", 30.0)
        assert risk.get_market_exposure("0xmkt1") == pytest.approx(20.0)

    def test_exposure_never_negative(self, risk):
        risk.record_order_opened("0xmkt1", 10.0)
        risk.record_order_closed("0xmkt1", 999.0)   # cerrar más de lo abierto
        assert risk.get_market_exposure("0xmkt1") == 0.0

    def test_total_exposure_across_markets(self, risk):
        risk.record_order_opened("0xmkt1", 40.0)
        risk.record_order_opened("0xmkt2", 60.0)
        assert risk.get_total_exposure() == pytest.approx(100.0)

    def test_unknown_market_exposure_is_zero(self, risk):
        assert risk.get_market_exposure("0xnonexistent") == 0.0


# ---------------------------------------------------------------------------
# Kelly sizing (max_order_size_usd)
# ---------------------------------------------------------------------------

class TestKellySizingRM:

    def test_basic_kelly_formula(self, risk):
        """Kelly = bankroll * kelly_fraction * edge * 2."""
        # half_spread=1¢, mid=0.50 → edge=0.02 → kelly=1000*0.25*0.02*2=$10
        size = risk.max_order_size_usd("0xmkt", half_spread_cents=1.0, mid_price=0.50)
        assert size == pytest.approx(10.0, abs=0.1)

    def test_hard_cap_per_order(self, risk):
        """Nunca supera max_order_risk_pct * bankroll ($50)."""
        # Spread enorme → Kelly daría mucho, pero tope es $50
        size = risk.max_order_size_usd("0xmkt", half_spread_cents=50.0, mid_price=0.50)
        assert size <= 50.0

    def test_exposure_cap_reduces_size(self, risk):
        """Con exposición alta, el size disponible se reduce."""
        risk.record_order_opened("0xmkt", 180.0)  # 18% de $1000, tope=20%
        # Solo quedan $20 de capacidad, dividido entre bid+ask = $10 c/u
        size = risk.max_order_size_usd("0xmkt", half_spread_cents=2.0, mid_price=0.50)
        assert size <= 10.0

    def test_full_exposure_returns_zero(self, risk):
        """Con exposición al 100%, size=0."""
        risk.record_order_opened("0xmkt", 200.0)  # 20% = tope máximo
        size = risk.max_order_size_usd("0xmkt", half_spread_cents=2.0, mid_price=0.50)
        assert size == 0.0

    def test_zero_mid_price_uses_fallback(self, risk):
        """Con mid=0 no divide por cero, usa el hard cap."""
        size = risk.max_order_size_usd("0xmkt", half_spread_cents=2.0, mid_price=0.0)
        assert size >= 0.0  # no lanza excepción


# ---------------------------------------------------------------------------
# P&L — registro de fills
# ---------------------------------------------------------------------------

class TestPnL:

    def test_buy_fill_updates_position(self, risk):
        """Un fill BUY acumula posición larga y calcula avg_entry_price."""
        risk.record_fill("0xmkt", "BUY", price=0.48, size=100)
        exp = risk._get_exp("0xmkt")
        assert exp.filled_yes == pytest.approx(100.0)
        assert exp.avg_entry_price == pytest.approx(0.48)

    def test_weighted_avg_entry_price(self, risk):
        """El precio medio se pondera correctamente en fills múltiples."""
        risk.record_fill("0xmkt", "BUY", price=0.40, size=100)  # $40
        risk.record_fill("0xmkt", "BUY", price=0.60, size=100)  # $60
        # Total cost=$100, total shares=200 → avg=0.50
        exp = risk._get_exp("0xmkt")
        assert exp.avg_entry_price == pytest.approx(0.50)

    def test_sell_fill_realizes_pnl(self, risk):
        """Un fill SELL tras un BUY captura el spread como P&L."""
        risk.record_fill("0xmkt", "BUY",  price=0.48, size=100)
        risk.record_fill("0xmkt", "SELL", price=0.52, size=100)
        # PnL = (0.52 - 0.48) * 100 = $4.00
        assert risk._session_pnl == pytest.approx(4.0, abs=0.01)

    def test_sell_without_position_no_pnl(self, risk):
        """Un fill SELL sin posición previa no genera P&L ficticio."""
        risk.record_fill("0xmkt", "SELL", price=0.52, size=50)
        assert risk._session_pnl == pytest.approx(0.0)

    def test_pnl_kill_switch_integration(self, risk):
        """Si los fills acumulan suficiente pérdida, el kill switch se activa."""
        # Simulamos pérdida: compramos caro, vendemos barato
        risk.record_fill("0xmkt", "BUY",  price=0.90, size=600)
        risk.record_fill("0xmkt", "SELL", price=0.80, size=600)
        # PnL = (0.80-0.90)*600 = -$60 → supera umbral de -$50
        triggered, _ = risk.check_kill_switch()
        assert triggered


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------

class TestStats:

    def test_stats_structure(self, risk):
        stats = risk.get_stats()
        assert "session_pnl_usd" in stats
        assert "total_exposure_usd" in stats
        assert "total_exposure_pct" in stats
        assert "consecutive_errors" in stats
        assert "kill_switch" in stats
        assert "uptime_min" in stats

    def test_stats_kill_switch_false_initially(self, risk):
        assert not risk.get_stats()["kill_switch"]

    def test_stats_exposure_pct_calculation(self, risk):
        risk.record_order_opened("0xmkt", 100.0)  # 10% de $1000
        stats = risk.get_stats()
        assert stats["total_exposure_pct"] == pytest.approx(10.0, abs=0.1)

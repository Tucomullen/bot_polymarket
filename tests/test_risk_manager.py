"""
tests/test_risk_manager.py — Tests del gestor de riesgo.
"""

import pytest
from src.risk_manager import RiskManager


# ---------------------------------------------------------------------------
# TestKillSwitch
# ---------------------------------------------------------------------------

class TestKillSwitch:
    """Valida los mecanismos de activación del kill switch."""

    def test_no_kill_switch_initially(self, risk: RiskManager):
        """El kill switch no está activo al iniciar."""
        triggered, reason = risk.check_kill_switch()
        assert triggered is False
        assert reason == ""

    def test_kill_switch_on_session_loss(self, risk: RiskManager):
        """El kill switch se activa cuando la pérdida de sesión supera el límite."""
        # bankroll=1000, max_session_loss_pct=0.05 → límite = 50$
        # Simular una pérdida de 60$
        risk._session_pnl = -60.0
        triggered, reason = risk.check_kill_switch()
        assert triggered is True
        assert "pérdida" in reason or "loss" in reason.lower() or len(reason) > 0

    def test_kill_switch_remains_active_after_trigger(self, risk: RiskManager):
        """Una vez activado el kill switch, permanece activo aunque el PnL mejore."""
        risk._session_pnl = -60.0
        risk.check_kill_switch()  # activa
        risk._session_pnl = 0.0  # "mejora" el PnL
        triggered, _ = risk.check_kill_switch()
        assert triggered is True

    def test_kill_switch_on_consecutive_errors(self, risk: RiskManager):
        """El kill switch se activa al acumular el máximo de errores consecutivos."""
        # max_consecutive_errors=5
        for _ in range(5):
            risk.record_error()
        triggered, reason = risk.check_kill_switch()
        assert triggered is True
        assert "error" in reason.lower() or len(reason) > 0

    def test_reset_kill_switch(self, risk: RiskManager):
        """reset_kill_switch desactiva el kill switch y limpia el contador de errores."""
        risk._session_pnl = -60.0
        risk.check_kill_switch()
        assert risk.check_kill_switch()[0] is True

        risk.reset_kill_switch()
        risk._session_pnl = 0.0
        triggered, _ = risk.check_kill_switch()
        assert triggered is False

    def test_record_success_resets_error_counter(self, risk: RiskManager):
        """record_success() limpia el contador de errores consecutivos."""
        risk.record_error()
        risk.record_error()
        risk.record_success()
        assert risk._consecutive_errors == 0


# ---------------------------------------------------------------------------
# TestExposureTracking
# ---------------------------------------------------------------------------

class TestExposureTracking:
    """Valida el seguimiento de exposición por mercado."""

    def test_exposure_increases_with_order(self, risk: RiskManager):
        """La exposición sube cuando se abre una orden."""
        risk.record_order_opened("mkt1", 25.0)
        assert risk.get_market_exposure("mkt1") == 25.0

    def test_exposure_decreases_on_close(self, risk: RiskManager):
        """La exposición baja cuando se cancela una orden."""
        risk.record_order_opened("mkt1", 25.0)
        risk.record_order_closed("mkt1", 25.0)
        assert risk.get_market_exposure("mkt1") == 0.0

    def test_exposure_never_negative(self, risk: RiskManager):
        """La exposición no puede ser negativa."""
        risk.record_order_opened("mkt1", 10.0)
        risk.record_order_closed("mkt1", 50.0)  # cierra más de lo que hay
        assert risk.get_market_exposure("mkt1") == 0.0

    def test_total_exposure_aggregates_markets(self, risk: RiskManager):
        """get_total_exposure() suma todos los mercados."""
        risk.record_order_opened("mkt1", 20.0)
        risk.record_order_opened("mkt2", 30.0)
        assert risk.get_total_exposure() == 50.0

    def test_unknown_market_exposure_is_zero(self, risk: RiskManager):
        """Un mercado sin datos tiene exposición cero."""
        assert risk.get_market_exposure("unknown_market") == 0.0


# ---------------------------------------------------------------------------
# TestKellySizing
# ---------------------------------------------------------------------------

class TestKellySizing:
    """Valida el cálculo de tamaño de orden con Kelly fraccional."""

    def test_kelly_formula(self, risk: RiskManager):
        """Kelly = bankroll * kelly_fraction * edge * 2."""
        half_spread_cents = 1.0
        mid = 0.50
        edge = (half_spread_cents / 100.0) / mid
        raw_kelly = 1000.0 * 0.25 * edge * 2.0
        result = risk.max_order_size_usd("mkt1", half_spread_cents, mid)
        # Limitado por max_order_risk_pct (0.05 * 1000 = 50)
        assert result <= min(raw_kelly, 50.0)

    def test_kelly_hard_cap_per_order(self, risk: RiskManager):
        """Con spread muy alto, Kelly está limitado por max_order_risk_pct."""
        size = risk.max_order_size_usd("mkt1", half_spread_cents=50.0, mid_price=0.50)
        assert size <= 1000.0 * 0.05  # 50$

    def test_kelly_reduced_by_existing_exposure(self, risk: RiskManager):
        """Con alta exposición existente, el tamaño se reduce."""
        # max_total_exposure = 20% de 1000 = 200$
        # Si ya tenemos 190$ abiertos, solo quedan 10$ / 2 = 5$ por lado
        risk.record_order_opened("mkt_other", 190.0)
        size = risk.max_order_size_usd("mkt1", half_spread_cents=1.0, mid_price=0.50)
        assert size <= 5.0

    def test_kelly_zero_when_exposure_maxed(self, risk: RiskManager):
        """Con exposición al máximo, Kelly devuelve 0."""
        risk.record_order_opened("mkt_other", 200.0)
        size = risk.max_order_size_usd("mkt1", half_spread_cents=1.0, mid_price=0.50)
        assert size == 0.0


# ---------------------------------------------------------------------------
# TestPnLCalculation
# ---------------------------------------------------------------------------

class TestPnLCalculation:
    """Valida el cálculo de P&L por fills."""

    def test_buy_fill_sets_entry_price(self, risk: RiskManager):
        """Un fill BUY establece el precio medio de entrada."""
        risk.record_fill("mkt1", "BUY", price=0.48, size=100.0)
        exp = risk._get_exp("mkt1")
        assert exp.avg_entry_price == pytest.approx(0.48)
        assert exp.filled_yes == pytest.approx(100.0)

    def test_sell_fill_realizes_pnl(self, risk: RiskManager):
        """Un fill SELL realiza P&L basado en la diferencia precio entrada/salida."""
        risk.record_fill("mkt1", "BUY", price=0.48, size=100.0)
        risk.record_fill("mkt1", "SELL", price=0.52, size=100.0)
        exp = risk._get_exp("mkt1")
        # PnL = (0.52 - 0.48) * 100 = 4.0$
        assert exp.realized_pnl == pytest.approx(4.0, abs=1e-6)
        assert risk._session_pnl == pytest.approx(4.0, abs=1e-6)

    def test_sell_without_position_no_pnl(self, risk: RiskManager):
        """Un fill SELL sin posición previa no genera P&L."""
        risk.record_fill("mkt1", "SELL", price=0.52, size=50.0)
        exp = risk._get_exp("mkt1")
        assert exp.realized_pnl == 0.0
        assert risk._session_pnl == 0.0

    def test_weighted_average_entry_price(self, risk: RiskManager):
        """Múltiples compras calculan el precio medio ponderado correctamente."""
        risk.record_fill("mkt1", "BUY", price=0.40, size=100.0)
        risk.record_fill("mkt1", "BUY", price=0.60, size=100.0)
        exp = risk._get_exp("mkt1")
        # (0.40*100 + 0.60*100) / 200 = 0.50
        assert exp.avg_entry_price == pytest.approx(0.50, abs=1e-6)


# ---------------------------------------------------------------------------
# TestKillSwitchFromFills
# ---------------------------------------------------------------------------

class TestKillSwitchFromFills:
    """Valida que los fills acumulados pueden activar el kill switch."""

    def test_fills_trigger_kill_switch(self, risk: RiskManager):
        """Si los fills acumulan suficiente pérdida, el kill switch se activa."""
        # bankroll=1000, max_session_loss_pct=0.05 → límite = 50$
        # Comprar caro y vender barato para generar pérdida
        risk.record_fill("mkt1", "BUY", price=0.90, size=600.0)
        risk.record_fill("mkt1", "SELL", price=0.80, size=600.0)
        # PnL = (0.80 - 0.90) * 600 = -60$ → supera el límite de -50$
        triggered, reason = risk.check_kill_switch()
        assert triggered is True


# ---------------------------------------------------------------------------
# TestStats
# ---------------------------------------------------------------------------

class TestStats:
    """Valida que get_stats() devuelve todas las métricas esperadas."""

    def test_stats_contains_expected_keys(self, risk: RiskManager):
        """get_stats() incluye todas las claves esperadas."""
        stats = risk.get_stats()
        expected_keys = {
            "session_pnl_usd",
            "total_exposure_usd",
            "total_exposure_pct",
            "consecutive_errors",
            "kill_switch",
            "uptime_min",
        }
        assert expected_keys.issubset(stats.keys())

    def test_stats_kill_switch_false_initially(self, risk: RiskManager):
        """Kill switch está en False en el estado inicial."""
        stats = risk.get_stats()
        assert stats["kill_switch"] is False

    def test_stats_reflect_session_pnl(self, risk: RiskManager):
        """session_pnl_usd refleja el P&L acumulado."""
        risk.record_fill("mkt1", "BUY", price=0.48, size=100.0)
        risk.record_fill("mkt1", "SELL", price=0.52, size=100.0)
        stats = risk.get_stats()
        assert stats["session_pnl_usd"] == pytest.approx(4.0, abs=1e-4)

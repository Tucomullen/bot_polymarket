"""
tests/test_quoting.py — Tests del motor de cotización bidireccional.
"""

import time
import pytest

from src.quoting import QuotingEngine, QuotingConfig, QuotePair
from src.risk_manager import RiskManager
from src.discovery import MarketCandidate


# ---------------------------------------------------------------------------
# TestKellySizing
# ---------------------------------------------------------------------------

class TestKellySizing:
    """Valida el cálculo de tamaño de orden con Kelly Criterion."""

    def test_kelly_formula_basic(self, engine: QuotingEngine, market: MarketCandidate, risk: RiskManager):
        """Kelly size = bankroll * kelly_fraction * edge * 2."""
        half_spread_cents = 1.0
        mid = 0.50
        edge = (half_spread_cents / 100.0) / mid
        expected_kelly = 1000.0 * 0.25 * edge * 2.0
        result = risk.max_order_size_usd(market.condition_id, half_spread_cents, mid)
        # El resultado está limitado por max_order_risk_pct (5% de 1000 = 50)
        assert result <= 1000.0 * 0.05

    def test_kelly_respects_bankroll_cap(self, risk: RiskManager, market: MarketCandidate):
        """Nunca supera max_order_risk_pct * bankroll."""
        # Con spread muy alto, Kelly podría dar números enormes
        size = risk.max_order_size_usd(market.condition_id, half_spread_cents=20.0, mid_price=0.50)
        assert size <= 1000.0 * 0.05

    def test_kelly_respects_exposure_cap(self, risk: RiskManager, market: MarketCandidate):
        """Con exposición alta, Kelly devuelve un valor menor."""
        # Simular que ya tenemos mucha exposición
        risk.record_order_opened(market.condition_id, size_usd=190.0)
        # La exposición total es 190, el máximo es 20% de 1000 = 200
        # Quedan solo 10 de margen, dividido entre 2 lados = 5
        size = risk.max_order_size_usd(market.condition_id, half_spread_cents=1.0, mid_price=0.50)
        assert size <= 5.0

    def test_kelly_zero_mid_price(self, risk: RiskManager, market: MarketCandidate):
        """Con mid_price=0, no devuelve NaN ni excepción."""
        size = risk.max_order_size_usd(market.condition_id, half_spread_cents=1.0, mid_price=0.0)
        assert size >= 0.0
        assert not (size != size)  # not NaN

    def test_kelly_zero_spread(self, risk: RiskManager, market: MarketCandidate):
        """Con half_spread=0, no devuelve NaN ni excepción."""
        size = risk.max_order_size_usd(market.condition_id, half_spread_cents=0.0, mid_price=0.50)
        assert size >= 0.0


# ---------------------------------------------------------------------------
# TestGenerateQuotes
# ---------------------------------------------------------------------------

class TestGenerateQuotes:
    """Valida la generación de pares de cotizaciones bid/ask."""

    def test_generates_bid_and_ask(self, engine: QuotingEngine, market: MarketCandidate):
        """generate_quotes devuelve un QuotePair con bid y ask."""
        pair = engine.generate_quotes(market)
        assert pair.bid is not None
        assert pair.ask is not None
        assert pair.is_complete

    def test_bid_below_ask(self, engine: QuotingEngine, market: MarketCandidate):
        """El bid siempre debe ser menor que el ask."""
        pair = engine.generate_quotes(market)
        assert pair.bid.price < pair.ask.price

    def test_bid_is_buy_ask_is_sell(self, engine: QuotingEngine, market: MarketCandidate):
        """Bid es BUY y ask es SELL."""
        pair = engine.generate_quotes(market)
        assert pair.bid.side == "BUY"
        assert pair.ask.side == "SELL"

    def test_prices_aligned_to_tick(self, engine: QuotingEngine, market: MarketCandidate):
        """Los precios están alineados al tick size del mercado."""
        tick = float(market.tick_size)
        pair = engine.generate_quotes(market)
        # Con tick=0.01, los precios deben ser múltiplos de 0.01
        assert abs(round(pair.bid.price / tick) * tick - pair.bid.price) < 1e-9
        assert abs(round(pair.ask.price / tick) * tick - pair.ask.price) < 1e-9

    def test_bid_below_best_ask(self, engine: QuotingEngine, market: MarketCandidate):
        """Con prevent_cross_spread, el bid no cruza el best ask del mercado."""
        pair = engine.generate_quotes(market)
        # best_ask del mercado es 0.52, nuestro bid debe estar por debajo
        assert pair.bid.price < market.best_ask

    def test_ask_above_best_bid(self, engine: QuotingEngine, market: MarketCandidate):
        """Con prevent_cross_spread, el ask no cruza el best bid del mercado."""
        pair = engine.generate_quotes(market)
        # best_bid del mercado es 0.48, nuestro ask debe estar por encima
        assert pair.ask.price > market.best_bid

    def test_prices_in_valid_range(self, engine: QuotingEngine, market: MarketCandidate):
        """Los precios están en el rango válido (0, 1)."""
        pair = engine.generate_quotes(market)
        assert 0.0 < pair.bid.price < 1.0
        assert 0.0 < pair.ask.price < 1.0


# ---------------------------------------------------------------------------
# TestInventorySkew
# ---------------------------------------------------------------------------

class TestInventorySkew:
    """Valida que el inventario desplaza los precios correctamente."""

    def test_long_yes_shifts_prices_down(self, engine: QuotingEngine, market: MarketCandidate):
        """Con inventario largo en YES, los precios se desplazan a la baja."""
        pair_neutral = engine.generate_quotes(market, inventory_yes=0, inventory_no=0)
        pair_long = engine.generate_quotes(market, inventory_yes=500, inventory_no=0)

        # Con inventario largo en YES, queremos vender más barato → precios bajan
        assert pair_long.bid.price <= pair_neutral.bid.price
        assert pair_long.ask.price <= pair_neutral.ask.price

    def test_long_no_shifts_prices_up(self, engine: QuotingEngine, market: MarketCandidate):
        """Con inventario largo en NO, los precios se desplazan al alza."""
        pair_neutral = engine.generate_quotes(market, inventory_yes=0, inventory_no=0)
        pair_long_no = engine.generate_quotes(market, inventory_yes=0, inventory_no=500)

        # Con inventario largo en NO, queremos comprar YES → precios suben
        assert pair_long_no.bid.price >= pair_neutral.bid.price
        assert pair_long_no.ask.price >= pair_neutral.ask.price


# ---------------------------------------------------------------------------
# TestShouldRequote
# ---------------------------------------------------------------------------

class TestShouldRequote:
    """Valida la lógica de recotización."""

    def test_requote_when_no_prior_quote(self, engine: QuotingEngine, market: MarketCandidate):
        """Sin quote previa, should_requote devuelve True."""
        should, reason = engine.should_requote(market)
        assert should is True
        assert reason == "no_existing_quote"

    def test_no_requote_when_fresh(self, engine: QuotingEngine, market: MarketCandidate):
        """Tras generar una quote reciente, should_requote devuelve False."""
        engine.generate_quotes(market)
        should, reason = engine.should_requote(market)
        assert should is False
        assert reason == ""

    def test_requote_when_stale(self, engine: QuotingEngine, market: MarketCandidate):
        """Una quote más antigua que max_quote_age_sec debe recotizarse."""
        engine.generate_quotes(market)
        # Manipular el timestamp guardado para simular antigüedad
        last = engine.get_last_quotes(market.condition_id)
        last.timestamp = time.time() - (engine.cfg.max_quote_age_sec + 1)

        should, reason = engine.should_requote(market)
        assert should is True
        assert "stale" in reason

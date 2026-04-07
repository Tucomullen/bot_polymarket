"""
tests/test_quoting.py — Tests del QuotingEngine.

Cubre: Kelly sizing, generación de quotes, protección anti-cross,
cuantización al tick, skew por inventario y lógica de re-cotización.
"""

import pytest
from src.quoting import QuotingEngine, QuotingConfig, Quote, QuotePair
from src.risk_manager import RiskManager


# ---------------------------------------------------------------------------
# Kelly sizing via _compute_order_size
# ---------------------------------------------------------------------------

class TestKellySizing:

    def test_kelly_formula(self, engine, market, risk):
        """Kelly size = bankroll * kelly_fraction * edge * 2."""
        half_spread_cents = 2.0   # spread de 4¢ → half = 2¢
        mid = 0.50
        # edge = (2/100) / 0.50 = 0.04  →  kelly = 1000 * 0.25 * 0.04 * 2 = $20
        size = engine._compute_order_size(market, 1000.0, half_spread_cents, mid, risk)
        assert 15.0 <= size <= 25.0, f"Kelly size esperado ~$20, obtenido {size}"

    def test_kelly_respects_hard_cap(self, engine, market):
        """Kelly no puede superar max_order_risk_pct * bankroll."""
        risk_strict = RiskManager(bankroll_usd=1000.0, kelly_fraction=1.0,
                                  max_order_risk_pct=0.05)
        size = engine._compute_order_size(market, 1000.0, 10.0, 0.50, risk_strict)
        assert size <= 50.0, "No debe superar el hard cap de $50 (5% de $1000)"

    def test_kelly_respects_exposure_cap(self, engine, market, risk):
        """Cuando la exposición total está llena, size debe ser 0."""
        # Llenar la exposición al máximo
        risk.record_order_opened(market.condition_id, 200.0)  # 20% de $1000
        size = engine._compute_order_size(market, 1000.0, 2.0, 0.50, risk)
        assert size == 0.0, "Con exposición llena, no debe abrirse más capital"

    def test_kelly_zero_spread_returns_zero(self, engine, market, risk):
        """Con spread 0, el edge es 0 → Kelly size es 0."""
        size = engine._compute_order_size(market, 1000.0, 0.0, 0.50, risk)
        assert size == 0.0

    def test_legacy_fallback_without_risk_manager(self, engine, market):
        """Sin risk_manager usa lógica legacy (cap 5% bankroll)."""
        size = engine._compute_order_size(market, 1000.0)
        assert size <= 50.0  # 5% de $1000
        assert size >= 5.0   # min_order_size_usd


# ---------------------------------------------------------------------------
# generate_quotes — Estructura básica
# ---------------------------------------------------------------------------

class TestGenerateQuotes:

    def test_returns_complete_pair(self, engine, market):
        """generate_quotes devuelve QuotePair con bid y ask."""
        pair = engine.generate_quotes(market, bankroll_usd=1000.0)
        assert pair.is_complete, "Debe generar bid Y ask"
        assert pair.bid is not None
        assert pair.ask is not None

    def test_bid_below_ask(self, engine, market):
        """El bid siempre debe estar por debajo del ask."""
        pair = engine.generate_quotes(market, bankroll_usd=1000.0)
        assert pair.bid.price < pair.ask.price

    def test_bid_side_is_buy(self, engine, market):
        assert engine.generate_quotes(market).bid.side == "BUY"

    def test_ask_side_is_sell(self, engine, market):
        assert engine.generate_quotes(market).ask.side == "SELL"

    def test_prices_on_tick_grid(self, engine, market):
        """Los precios deben estar en múltiplos del tick size (0.01)."""
        pair = engine.generate_quotes(market, bankroll_usd=1000.0)
        tick = float(market.tick_size)
        bid_rem = round(pair.bid.price % tick, 6)
        ask_rem = round(pair.ask.price % tick, 6)
        assert bid_rem < 1e-9 or abs(bid_rem - tick) < 1e-9, \
            f"Bid {pair.bid.price} no está en el grid de tick {tick}"
        assert ask_rem < 1e-9 or abs(ask_rem - tick) < 1e-9, \
            f"Ask {pair.ask.price} no está en el grid de tick {tick}"

    def test_no_cross_with_best_market(self, engine, market):
        """El bid no cruza el best_ask del mercado y viceversa."""
        pair = engine.generate_quotes(market, bankroll_usd=1000.0)
        # best_ask real del orderbook = 0.52, nuestro bid debe estar por debajo
        assert pair.bid.price < market.best_ask
        assert pair.ask.price > market.best_bid

    def test_invalid_mid_returns_empty(self, engine, market):
        """Mid = 0 o 1 → QuotePair vacío (no se puede cotizar)."""
        from dataclasses import replace
        # Usar token_id que NO esté en el orderbook fixture para forzar uso del midpoint=0.0
        market_decided = replace(market, midpoint=0.0, best_bid=0.0, best_ask=0.0,
                                 token_id_yes="0xunknown_token", token_id_no="0xunknown_no")
        pair = engine.generate_quotes(market_decided, bankroll_usd=1000.0)
        assert not pair.is_complete


# ---------------------------------------------------------------------------
# Skew por inventario
# ---------------------------------------------------------------------------

class TestInventorySkew:

    def test_long_yes_shifts_prices_down(self, engine, market):
        """Con inventario largo en YES, los precios se desplazan a la baja."""
        pair_neutral = engine.generate_quotes(market, inventory_yes=0, bankroll_usd=1000.0)
        pair_long    = engine.generate_quotes(market, inventory_yes=500, bankroll_usd=1000.0)
        assert pair_long.bid.price <= pair_neutral.bid.price, \
            "Con inventario largo, el bid debe bajar para frenar compras"

    def test_no_skew_when_factor_zero(self, engine, market):
        """Con inventory_skew_factor=0, inventario no afecta los precios."""
        from src.quoting import QuotingConfig
        from src.orderbook import OrderbookTracker
        engine_no_skew = QuotingEngine(
            cfg=QuotingConfig(inventory_skew_factor=0.0),
            orderbook=engine._orderbook,
        )
        pair_neutral = engine_no_skew.generate_quotes(market, inventory_yes=0)
        pair_long    = engine_no_skew.generate_quotes(market, inventory_yes=1000)
        assert pair_neutral.bid.price == pair_long.bid.price


# ---------------------------------------------------------------------------
# should_requote
# ---------------------------------------------------------------------------

class TestShouldRequote:

    def test_requote_on_first_call(self, engine, market):
        """Sin quote previa, siempre se debe recotizar."""
        should, reason = engine.should_requote(market)
        assert should
        assert reason == "no_existing_quote"

    def test_no_requote_after_fresh_quote(self, engine, market):
        """Tras generar una quote, no se recotiza inmediatamente."""
        engine.generate_quotes(market, bankroll_usd=1000.0)
        should, _ = engine.should_requote(market)
        assert not should

    def test_requote_when_stale(self, engine, market):
        """Se recotiza si la quote supera max_quote_age_sec."""
        import time
        pair = engine.generate_quotes(market, bankroll_usd=1000.0)
        # Retroceder el timestamp para simular quote vieja
        pair.timestamp -= 10.0
        engine._last_quotes[market.condition_id] = pair
        should, reason = engine.should_requote(market)
        assert should
        assert "stale" in reason

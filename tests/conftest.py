"""
tests/conftest.py — Fixtures compartidos para los tests del bot.
"""

import pytest
from src.discovery import MarketCandidate, MarketCategory
from src.orderbook import OrderbookTracker
from src.quoting import QuotingEngine, QuotingConfig
from src.risk_manager import RiskManager


@pytest.fixture
def market() -> MarketCandidate:
    """MarketCandidate de referencia con valores controlados."""
    return MarketCandidate(
        condition_id="0xtest_condition_id",
        question="¿Subirá BTC por encima de $100k esta semana?",
        slug="btc-100k-this-week",
        token_id_yes="0xtest_token_yes",
        token_id_no="0xtest_token_no",
        tick_size="0.01",
        category=MarketCategory.CRYPTO_SHORT_TERM,
        best_bid=0.48,
        best_ask=0.52,
        spread_cents=4.0,
        midpoint=0.50,
        volume_24h=50000.0,
        hours_to_resolution=24.0,
        has_maker_rebates=True,
        has_liquidity_rewards=True,
        daily_reward_usd=50.0,
        reward_max_spread=5.0,
        reward_min_size=10.0,
        score=75.0,
    )


@pytest.fixture
def orderbook() -> OrderbookTracker:
    """OrderbookTracker con datos sintéticos para el mercado de referencia."""
    tracker = OrderbookTracker()
    tracker.process_book_event({
        "asset_id": "0xtest_token_yes",
        "bids": [{"price": "0.48", "size": "500"}],
        "asks": [{"price": "0.52", "size": "500"}],
    })
    return tracker


@pytest.fixture
def engine(orderbook: OrderbookTracker) -> QuotingEngine:
    """QuotingEngine con configuración de referencia."""
    return QuotingEngine(
        cfg=QuotingConfig(
            base_spread_cents=2.0,
            min_spread_cents=0.5,
            max_spread_cents=6.0,
            inventory_skew_factor=0.3,
            base_order_size_usd=25.0,
            min_order_size_usd=5.0,
            max_order_size_usd=200.0,
            prevent_cross_spread=True,
            max_quote_age_sec=5.0,
        ),
        orderbook=orderbook,
    )


@pytest.fixture
def risk() -> RiskManager:
    """RiskManager con bankroll de $1000 y parámetros estándar."""
    return RiskManager(
        bankroll_usd=1000.0,
        kelly_fraction=0.25,
        max_order_risk_pct=0.05,
        max_total_exposure_pct=0.20,
        max_session_loss_pct=0.05,
        max_consecutive_errors=5,
    )

"""
backtesting/simulator.py — Motor de simulación de market making sobre datos históricos.

Metodología:
  Para cada par de puntos consecutivos (precio_t, precio_t+1):
    1. Inyectar precio_t en el OrderbookTracker como book sintético
    2. Llamar a QuotingEngine.generate_quotes() → obtener bid/ask
    3. Si precio_t+1 <= nuestro bid → fill BUY simulado
    4. Si precio_t+1 >= nuestro ask → fill SELL simulado
    5. Registrar posición, P&L y drawdown

Limitaciones conocidas:
  - Granularidad horaria: no refleja el comportamiento tick-a-tick real
  - No hay impacto de mercado ni slippage
  - Fill rate optimista vs. ejecución real (mejor como cota superior)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from backtesting.downloader import MarketHistory, PricePoint
from src.discovery import MarketCandidate, MarketCategory
from src.orderbook import OrderbookTracker
from src.quoting import QuotingEngine, QuotingConfig
from src.risk_manager import RiskManager

logger = logging.getLogger("polybot.backtest.simulator")


# ---------------------------------------------------------------------------
# Estructuras de datos
# ---------------------------------------------------------------------------

@dataclass
class FillEvent:
    """Un fill simulado en el backtest."""
    step: int
    timestamp: float
    side: str         # "BUY" o "SELL"
    price: float
    size: float
    size_usd: float
    pnl_this_fill: float = 0.0


@dataclass
class SimStep:
    """Estado del simulador en un paso de tiempo."""
    step: int
    timestamp: float
    mid_price: float
    our_bid: float
    our_ask: float
    position_yes: float     # Shares YES en cartera
    position_usd: float     # Valor de la posición en USD
    cumulative_pnl: float
    drawdown: float


@dataclass
class BacktestResult:
    """Resultado completo del backtest de un mercado."""
    condition_id: str
    question: str
    n_steps: int = 0
    n_fills: int = 0
    n_buy_fills: int = 0
    n_sell_fills: int = 0
    total_volume_usd: float = 0.0
    final_pnl: float = 0.0
    max_drawdown: float = 0.0        # En USD (negativo)
    max_drawdown_pct: float = 0.0    # En % del bankroll inicial
    peak_pnl: float = 0.0
    bankroll_initial: float = 0.0
    fills: list[FillEvent] = field(default_factory=list)
    steps: list[SimStep] = field(default_factory=list)

    @property
    def fill_rate(self) -> float:
        """% de pasos donde hubo al menos un fill."""
        return self.n_fills / max(self.n_steps, 1) * 100

    @property
    def return_pct(self) -> float:
        return self.final_pnl / max(self.bankroll_initial, 1) * 100


# ---------------------------------------------------------------------------
# Simulador
# ---------------------------------------------------------------------------

def _make_candidate(history: MarketHistory, price: float) -> MarketCandidate:
    """
    Construye un MarketCandidate sintético con el precio actual.
    Se usa en cada paso de tiempo para que QuotingEngine pueda cotizar.
    """
    tick = history.tick_size
    half_tick = tick / 2.0
    best_bid = max(tick, round(price - half_tick, 4))
    best_ask = min(1.0 - tick, round(price + half_tick, 4))
    return MarketCandidate(
        condition_id=history.condition_id,
        question=history.question,
        token_id_yes=history.token_id_yes,
        token_id_no="",
        tick_size=str(tick),
        category=MarketCategory.UNKNOWN,
        best_bid=best_bid,
        best_ask=best_ask,
        spread_cents=round((best_ask - best_bid) * 100, 2),
        midpoint=price,
        hours_to_resolution=24.0,
        has_maker_rebates=history.has_maker_rebates,
        has_liquidity_rewards=history.has_liquidity_rewards,
        daily_reward_usd=0.0,
        reward_max_spread=history.reward_max_spread,
        reward_min_size=history.reward_min_size,
        score=history.score,
    )


def _inject_book(tracker: OrderbookTracker, token_id: str, price: float, tick: float) -> None:
    """
    Inyecta un orderbook sintético basado en el precio histórico.
    El spread sintético es de 2 ticks para simular liquidez.
    """
    spread = tick * 2
    bid = max(tick, price - spread / 2)
    ask = min(1.0 - tick, price + spread / 2)
    tracker.process_book_event({
        "asset_id": token_id,
        "bids": [{"price": str(round(bid, 4)), "size": "500"}],
        "asks": [{"price": str(round(ask, 4)), "size": "500"}],
    })


def simulate_market(
    history: MarketHistory,
    bankroll_usd: float = 1000.0,
    quoting_cfg: QuotingConfig | None = None,
    risk_manager: RiskManager | None = None,
) -> BacktestResult:
    """
    Simula la estrategia de market making sobre el historial de un mercado.

    Args:
        history: historial de precios descargado
        bankroll_usd: capital inicial simulado
        quoting_cfg: config del motor de cotización (usa defaults si es None)
        risk_manager: gestor de riesgo (se crea uno con defaults si es None)

    Returns:
        BacktestResult con todos los fills, steps y métricas brutas
    """
    prices = history.prices
    if len(prices) < 2:
        logger.warning("⚠️  Datos insuficientes para %s (%d puntos)", history.condition_id[:16], len(prices))
        return BacktestResult(
            condition_id=history.condition_id,
            question=history.question,
            bankroll_initial=bankroll_usd,
        )

    if risk_manager is None:
        risk_manager = RiskManager(
            bankroll_usd=bankroll_usd,
            kelly_fraction=0.25,
            max_order_risk_pct=0.05,
            max_total_exposure_pct=0.20,
            max_session_loss_pct=0.10,   # más permisivo para no cortar el backtest
            max_consecutive_errors=999,
        )

    cfg = quoting_cfg or QuotingConfig()
    orderbook = OrderbookTracker()
    engine = QuotingEngine(cfg=cfg, orderbook=orderbook)

    result = BacktestResult(
        condition_id=history.condition_id,
        question=history.question,
        bankroll_initial=bankroll_usd,
    )

    # Estado de posición durante la simulación
    position_yes: float = 0.0       # shares YES en cartera
    avg_entry_price: float = 0.0    # precio medio de entrada
    cumulative_pnl: float = 0.0
    peak_pnl: float = 0.0
    max_drawdown: float = 0.0

    for i in range(len(prices) - 1):
        current = prices[i]
        next_pt = prices[i + 1]

        # Inyectar precio actual en el orderbook sintético
        _inject_book(orderbook, history.token_id_yes, current.price, history.tick_size)

        # Construir MarketCandidate con el precio actual
        candidate = _make_candidate(history, current.price)

        # Generar cotizaciones
        pair = engine.generate_quotes(
            candidate,
            inventory_yes=position_yes,
            bankroll_usd=bankroll_usd,
            risk_manager=risk_manager,
        )

        if not pair.is_complete:
            continue

        our_bid = pair.bid.price
        our_ask = pair.ask.price
        size_usd = pair.bid.size_usd  # mismo size_usd para bid y ask
        result.n_steps += 1

        # --- Simulación de fills ---
        # BUY fill: el precio bajó hasta nuestro bid (alguien nos vende)
        if next_pt.price <= our_bid and size_usd > 0:
            shares_bought = size_usd / our_bid

            # Actualizar posición y precio medio
            total_cost = avg_entry_price * position_yes + our_bid * shares_bought
            position_yes += shares_bought
            avg_entry_price = total_cost / position_yes if position_yes > 0 else 0.0

            risk_manager.record_fill(history.condition_id, "BUY", our_bid, shares_bought)

            fill = FillEvent(
                step=i,
                timestamp=current.timestamp,
                side="BUY",
                price=our_bid,
                size=shares_bought,
                size_usd=size_usd,
            )
            result.fills.append(fill)
            result.n_buy_fills += 1
            result.total_volume_usd += size_usd

        # SELL fill: el precio subió hasta nuestro ask (alguien nos compra)
        if next_pt.price >= our_ask and position_yes > 0 and size_usd > 0:
            shares_sold = min(size_usd / our_ask, position_yes)
            pnl = (our_ask - avg_entry_price) * shares_sold
            cumulative_pnl += pnl

            risk_manager.record_fill(history.condition_id, "SELL", our_ask, shares_sold)

            position_yes = max(0.0, position_yes - shares_sold)
            if position_yes == 0:
                avg_entry_price = 0.0

            fill = FillEvent(
                step=i,
                timestamp=current.timestamp,
                side="SELL",
                price=our_ask,
                size=shares_sold,
                size_usd=shares_sold * our_ask,
                pnl_this_fill=pnl,
            )
            result.fills.append(fill)
            result.n_sell_fills += 1
            result.total_volume_usd += shares_sold * our_ask

            # Actualizar drawdown
            if cumulative_pnl > peak_pnl:
                peak_pnl = cumulative_pnl
            drawdown = cumulative_pnl - peak_pnl
            if drawdown < max_drawdown:
                max_drawdown = drawdown

        # Registrar estado del step
        position_usd = position_yes * current.price
        result.steps.append(SimStep(
            step=i,
            timestamp=current.timestamp,
            mid_price=current.price,
            our_bid=our_bid,
            our_ask=our_ask,
            position_yes=position_yes,
            position_usd=position_usd,
            cumulative_pnl=cumulative_pnl,
            drawdown=cumulative_pnl - peak_pnl,
        ))

    # Liquidar posición final al último precio (mark-to-market)
    if position_yes > 0 and prices:
        last_price = prices[-1].price
        mark_pnl = (last_price - avg_entry_price) * position_yes
        cumulative_pnl += mark_pnl
        logger.debug("   Mark-to-market posición %.2f sh @ %.3f → PnL mtm=%.4f",
                     position_yes, last_price, mark_pnl)

    result.n_fills = result.n_buy_fills + result.n_sell_fills
    result.final_pnl = cumulative_pnl
    result.peak_pnl = peak_pnl
    result.max_drawdown = max_drawdown
    result.max_drawdown_pct = (abs(max_drawdown) / bankroll_usd * 100) if bankroll_usd > 0 else 0.0

    logger.info(
        "✅ %s | steps=%d fills=%d(B:%d/S:%d) PnL=%.4f DD=%.1f%%",
        history.question[:35],
        result.n_steps,
        result.n_fills,
        result.n_buy_fills,
        result.n_sell_fills,
        result.final_pnl,
        result.max_drawdown_pct,
    )
    return result


def run_backtest(
    histories: list[MarketHistory],
    bankroll_usd: float = 1000.0,
    quoting_cfg: QuotingConfig | None = None,
) -> list[BacktestResult]:
    """
    Ejecuta el backtest sobre todos los mercados descargados.
    Cada mercado usa su propio RiskManager independiente.
    """
    results = []
    for history in histories:
        logger.info("🔄 Simulando: %s", history.question[:50])
        result = simulate_market(
            history,
            bankroll_usd=bankroll_usd,
            quoting_cfg=quoting_cfg,
        )
        results.append(result)
    return results

"""
src/quoting.py — Motor de Cotización Bidireccional (Quoting Engine).

Calcula los precios y tamaños de las órdenes Maker a publicar en cada
mercado seleccionado. No coloca órdenes directamente — devuelve "quotes"
que el Order Manager se encarga de ejecutar.

Principios:
  - Siempre publica BID + ASK simultáneamente (cotización bidireccional)
  - Anclaje al midpoint del orderbook (o fair value externo en futuras versiones)
  - Spread dinámico: se ensancha con volatilidad/riesgo y se estrecha con poca competencia
  - Tamaño dinámico: escalado por score del mercado y nivel de inventario
  - Nunca cruza el spread (protección contra ser taker accidentalmente)
  - V1: 1 nivel por lado. Preparado para múltiples niveles en el futuro.
"""

import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.orderbook import OrderbookTracker
from src.discovery import MarketCandidate

if TYPE_CHECKING:
    from src.risk_manager import RiskManager

logger = logging.getLogger("polybot.quoting")


# ---------------------------------------------------------------------------
# Configuración del Quoting Engine
# ---------------------------------------------------------------------------

@dataclass
class QuotingConfig:
    """Parámetros configurables del motor de cotización."""

    # Spread (en centavos)
    base_spread_cents: float = 2.0
    min_spread_cents: float = 0.5
    max_spread_cents: float = 6.0

    # Ajustes dinámicos del spread
    inventory_skew_factor: float = 0.3    # 0=off, 1=agresivo: desplaza el mid según inventario
    volatility_multiplier: float = 1.5    # Ensanchar spread cuando hay volatilidad

    # Tamaño de órdenes (en USD)
    base_order_size_usd: float = 25.0
    min_order_size_usd: float = 5.0
    max_order_size_usd: float = 200.0
    scale_size_by_score: bool = True      # Escalar tamaño según score del mercado

    # Protecciones
    prevent_cross_spread: bool = True     # Nunca cruzar el spread
    min_edge_cents: float = 0.1           # Distancia mínima al best bid/ask contrario

    # Recotización
    requote_threshold_cents: float = 0.5  # Recotizar si el mid se mueve más de esto
    max_quote_age_sec: float = 5.0        # Forzar recotización si la quote tiene más de 5s
    level_r_max_quote_age_sec: float = 30.0  # Level R: recotizar solo cada 30s (menos churn)


# ---------------------------------------------------------------------------
# Estructuras de datos
# ---------------------------------------------------------------------------

@dataclass
class Quote:
    """Una cotización individual (una orden que queremos tener viva)."""
    token_id: str
    side: str            # "BUY" o "SELL"
    price: float
    size: float          # En acciones (shares)
    size_usd: float      # Equivalente en USD
    reason: str = ""     # Por qué se generó esta quote

    @property
    def is_bid(self) -> bool:
        return self.side == "BUY"

    @property
    def is_ask(self) -> bool:
        return self.side == "SELL"


@dataclass
class QuotePair:
    """Par de cotizaciones bid+ask para un mercado."""
    market: MarketCandidate
    bid: Quote | None = None
    ask: Quote | None = None
    timestamp: float = 0.0
    mid_at_generation: float = 0.0

    @property
    def is_complete(self) -> bool:
        """True si tenemos bid Y ask (cotización bidireccional completa)."""
        return self.bid is not None and self.ask is not None

    @property
    def age_sec(self) -> float:
        return time.time() - self.timestamp if self.timestamp else 999.0


# ---------------------------------------------------------------------------
# Motor de Cotización
# ---------------------------------------------------------------------------

class QuotingEngine:
    """
    Genera pares de cotizaciones bid/ask para mercados seleccionados.

    Uso:
        engine = QuotingEngine(config, orderbook_tracker)
        quotes = engine.generate_quotes(market, inventory_yes=50, inventory_no=30)
        # quotes.bid → Quote(side="BUY", price=0.48, size=50)
        # quotes.ask → Quote(side="SELL", price=0.52, size=50)
    """

    def __init__(
        self,
        cfg: QuotingConfig | None = None,
        orderbook: OrderbookTracker | None = None,
    ):
        self.cfg = cfg or QuotingConfig()
        self._orderbook = orderbook or OrderbookTracker()
        self._last_quotes: dict[str, QuotePair] = {}  # condition_id → última quote
        self._volatility_cache: dict[str, float] = {}  # token_id → volatilidad estimada

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def generate_quotes(
        self,
        market: MarketCandidate,
        inventory_yes: float = 0.0,
        inventory_no: float = 0.0,
        bankroll_usd: float = 1000.0,
        risk_manager: "RiskManager | None" = None,
    ) -> QuotePair:
        """
        Genera un par de cotizaciones bid/ask para el mercado dado.

        Args:
            market: mercado candidato del discovery
            inventory_yes: shares de YES que tenemos actualmente
            inventory_no: shares de NO que tenemos actualmente
            bankroll_usd: capital total disponible

        Returns:
            QuotePair con bid y ask (o None en alguno si no se puede cotizar)
        """
        # 1. Obtener estado del orderbook
        book_yes = self._orderbook.get(market.token_id_yes)

        # Usar datos del book si están frescos Y el libro es real (spread ≤ 50c).
        # Para Level R (y outrights en general) el CLOB muestra bid=0.001/ask=0.999:
        # ignorar esos datos y usar el midpoint de outcomePrices del discovery.
        _book_is_real = (
            book_yes is not None
            and book_yes.last_update > 0
            and book_yes.best_bid > 0
            and (book_yes.best_ask - book_yes.best_bid) <= 0.50
        )
        if _book_is_real:
            mid = book_yes.midpoint
            best_bid = book_yes.best_bid
            best_ask = book_yes.best_ask
        else:
            mid = market.midpoint
            best_bid = 0.0   # Sin referencia de libro real → la protección anti-cruce se omite
            best_ask = 0.0

        if mid <= 0 or mid >= 1:
            logger.debug("⚠️  Mid inválido (%.3f) para %s", mid, market.question[:40])
            return QuotePair(market=market)

        # 2. Calcular spread dinámico
        half_spread = self._compute_half_spread(market)

        # 3. Calcular skew por inventario
        skew = self._compute_inventory_skew(inventory_yes, inventory_no)

        # 4. Calcular precios bid y ask
        bid_price = mid - half_spread + skew
        ask_price = mid + half_spread + skew

        # 5. Cuantizar al tick size del mercado
        tick = float(market.tick_size) if market.tick_size else 0.01
        bid_price = self._quantize(bid_price, tick, direction="down")
        ask_price = self._quantize(ask_price, tick, direction="up")

        # 6. Protección: no cruzar el spread
        if self.cfg.prevent_cross_spread:
            min_edge = self.cfg.min_edge_cents / 100.0
            if best_ask > 0 and bid_price >= best_ask - min_edge:
                bid_price = self._quantize(best_ask - min_edge - tick, tick, "down")
            if best_bid > 0 and ask_price <= best_bid + min_edge:
                ask_price = self._quantize(best_bid + min_edge + tick, tick, "up")

        # 7. Validar rango de precios
        bid_price = max(tick, min(bid_price, 1.0 - tick))
        ask_price = max(tick, min(ask_price, 1.0 - tick))

        if bid_price >= ask_price:
            logger.debug("⚠️  bid >= ask tras ajustes, no se cotiza: bid=%.3f ask=%.3f", bid_price, ask_price)
            return QuotePair(market=market)

        # 8. Calcular tamaño (Kelly si hay risk_manager, otherwise lógica legacy)
        half_spread_cents = (ask_price - bid_price) * 100 / 2.0
        size_usd = self._compute_order_size(
            market, bankroll_usd, half_spread_cents, mid, risk_manager
        )
        bid_shares = size_usd / bid_price if bid_price > 0 else 0
        ask_shares = size_usd / ask_price if ask_price > 0 else 0

        # 9. Construir quotes
        bid_quote = Quote(
            token_id=market.token_id_yes,
            side="BUY",
            price=round(bid_price, 4),
            size=round(bid_shares, 2),
            size_usd=round(size_usd, 2),
            reason="maker_bid",
        )

        ask_quote = Quote(
            token_id=market.token_id_yes,
            side="SELL",
            price=round(ask_price, 4),
            size=round(ask_shares, 2),
            size_usd=round(size_usd, 2),
            reason="maker_ask",
        )

        pair = QuotePair(
            market=market,
            bid=bid_quote,
            ask=ask_quote,
            timestamp=time.time(),
            mid_at_generation=mid,
        )

        self._last_quotes[market.condition_id] = pair

        logger.info(
            "📐 Quote generada — %s | bid=%.3f (%d sh) | ask=%.3f (%d sh) | mid=%.3f | spread=%.1f¢",
            market.question[:40],
            bid_price,
            int(bid_shares),
            ask_price,
            int(ask_shares),
            mid,
            (ask_price - bid_price) * 100,
        )

        return pair

    def should_requote(
        self,
        market: MarketCandidate,
    ) -> tuple[bool, str]:
        """
        Determina si debemos recotizar un mercado.

        Returns:
            (should_requote, reason)
        """
        last = self._last_quotes.get(market.condition_id)

        if not last or not last.is_complete:
            return True, "no_existing_quote"

        # Edad de la quote — Level R recotiza más despacio (menos churn, libro vacío)
        is_level_r = getattr(market, "market_level", 0) == 3
        max_age = self.cfg.level_r_max_quote_age_sec if is_level_r else self.cfg.max_quote_age_sec
        if last.age_sec > max_age:
            return True, f"quote_stale_{last.age_sec:.1f}s"

        # Movimiento del midpoint — solo para libros reales (no placeholder)
        if not is_level_r:
            book = self._orderbook.get(market.token_id_yes)
            if book and book.last_update > 0 and book.best_bid > 0:
                if (book.best_ask - book.best_bid) <= 0.50:  # libro real
                    current_mid = book.midpoint
                    delta_cents = abs(current_mid - last.mid_at_generation) * 100
                    if delta_cents >= self.cfg.requote_threshold_cents:
                        return True, f"mid_moved_{delta_cents:.1f}c"

        return False, ""

    def get_last_quotes(self, condition_id: str) -> QuotePair | None:
        return self._last_quotes.get(condition_id)

    # ------------------------------------------------------------------
    # Cálculos internos
    # ------------------------------------------------------------------

    def _compute_half_spread(self, market: MarketCandidate) -> float:
        """
        Calcula el half-spread dinámico (mitad del spread total).
        """
        half = self.cfg.base_spread_cents / 100.0 / 2.0

        # Si el mercado tiene rewards con max_spread, no exceder ese límite
        if market.reward_max_spread > 0:
            max_half = market.reward_max_spread / 100.0 / 2.0
            half = min(half, max_half * 0.8)  # 80% del máximo para tener margen

        # Limitar al rango configurado
        min_half = self.cfg.min_spread_cents / 100.0 / 2.0
        max_half = self.cfg.max_spread_cents / 100.0 / 2.0
        half = max(min_half, min(half, max_half))

        return half

    def _compute_inventory_skew(
        self, inventory_yes: float, inventory_no: float
    ) -> float:
        """
        Calcula el desplazamiento (skew) del midpoint según inventario.
        """
        if self.cfg.inventory_skew_factor == 0:
            return 0.0

        net_inventory = inventory_yes - inventory_no
        total = abs(inventory_yes) + abs(inventory_no) + 1  # +1 para evitar div/0

        # Normalizar a [-1, 1]
        imbalance = net_inventory / total

        # Skew máximo: 2 centavos
        max_skew = 0.02
        skew = -imbalance * max_skew * self.cfg.inventory_skew_factor

        return skew

    def _compute_order_size(
        self,
        market: MarketCandidate,
        bankroll_usd: float,
        half_spread_cents: float = 1.0,
        mid_price: float = 0.5,
        risk_manager: "RiskManager | None" = None,
    ) -> float:
        """
        Calcula el tamaño de la orden en USD.
        """
        if risk_manager is not None:
            # Kelly fraccional: el risk_manager considera exposición total y Kelly
            kelly_max = risk_manager.max_order_size_usd(
                market.condition_id, half_spread_cents, mid_price
            )
            # Escalar por score (mejor mercado → más del presupuesto Kelly)
            if self.cfg.scale_size_by_score and market.score > 0:
                score_factor = market.score / 70.0
                kelly_max *= max(0.3, min(score_factor, 2.0))
            # Respetar min_size de rewards
            if market.reward_min_size > 0:
                kelly_max = max(kelly_max, market.reward_min_size)
            size = max(self.cfg.min_order_size_usd, min(kelly_max, self.cfg.max_order_size_usd))
        else:
            # Lógica legacy: base_order_size escalada por score, cap 5% bankroll
            size = self.cfg.base_order_size_usd
            if self.cfg.scale_size_by_score and market.score > 0:
                score_factor = market.score / 70.0
                size *= max(0.3, min(score_factor, 2.0))
            if market.reward_min_size > 0:
                size = max(size, market.reward_min_size)
            size = max(self.cfg.min_order_size_usd, min(size, self.cfg.max_order_size_usd))
            size = min(size, bankroll_usd * 0.05)

        level = getattr(market, "market_level", 1)

        # Nivel 2: libro de menor calidad → reducir tamaño al 50%
        if level == 2:
            size *= 0.5
            size = max(self.cfg.min_order_size_usd, size)

        # Nivel R: reward farming — tamaño mínimo fijo para limitar adverse selection.
        # El beneficio viene de los rewards, no del spread capturado.
        elif level == 3:
            size = self.cfg.min_order_size_usd
            if market.reward_min_size > 0:
                size = max(size, market.reward_min_size)

        return size

    @staticmethod
    def _quantize(price: float, tick: float, direction: str = "nearest") -> float:
        """Redondea un precio al tick size más cercano."""
        if tick <= 0:
            return price
        if direction == "down":
            return (int(price / tick)) * tick
        elif direction == "up":
            return math.ceil(price / tick) * tick
        else:
            return round(price / tick) * tick

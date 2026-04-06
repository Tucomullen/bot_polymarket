"""
src/orderbook.py — Estado local del libro de órdenes (Orderbook).

Mantiene una copia local del orderbook actualizada por los eventos
del WebSocket (canal market). El bot consulta este estado para
determinar el midpoint, best bid/ask y spread en tiempo real.
"""

import logging
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

logger = logging.getLogger("polybot.orderbook")


@dataclass
class OrderLevel:
    """Un nivel de precio en el libro."""
    price: float
    size: float


@dataclass
class OrderbookState:
    """Estado actual del libro de órdenes para un token."""
    token_id: str
    bids: list[OrderLevel] = field(default_factory=list)  # Ordenados desc por precio
    asks: list[OrderLevel] = field(default_factory=list)  # Ordenados asc por precio
    last_update: float = 0.0
    last_trade_price: float = 0.0

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    @property
    def midpoint(self) -> float:
        if self.bids and self.asks:
            return (self.best_bid + self.best_ask) / 2.0
        return 0.5

    @property
    def spread(self) -> float:
        """Spread en centavos."""
        return round((self.best_ask - self.best_bid) * 100, 2)

    @property
    def spread_pct(self) -> float:
        mid = self.midpoint
        if mid == 0:
            return 0.0
        return (self.best_ask - self.best_bid) / mid * 100


class OrderbookTracker:
    """
    Gestiona el estado local del orderbook para múltiples tokens.
    Thread-safe para acceso concurrente desde WebSocket y el bucle de trading.
    """

    def __init__(self):
        self._books: dict[str, OrderbookState] = {}
        self._lock = Lock()

    def get(self, token_id: str) -> OrderbookState | None:
        with self._lock:
            return self._books.get(token_id)

    def get_or_create(self, token_id: str) -> OrderbookState:
        with self._lock:
            if token_id not in self._books:
                self._books[token_id] = OrderbookState(token_id=token_id)
            return self._books[token_id]

    def process_book_event(self, data: dict[str, Any]) -> None:
        """
        Procesa un evento 'book' del WebSocket market channel.
        Estructura esperada:
        {
          "event_type": "book",
          "asset_id": "<token_id>",
          "bids": [{"price": "0.55", "size": "100"}, ...],
          "asks": [{"price": "0.57", "size": "80"}, ...],
          "hash": "...",
          "timestamp": "..."
        }
        """
        token_id = data.get("asset_id", "")
        if not token_id:
            return

        raw_bids = data.get("bids", [])
        raw_asks = data.get("asks", [])

        bids = sorted(
            [OrderLevel(price=float(b["price"]), size=float(b["size"])) for b in raw_bids],
            key=lambda x: x.price,
            reverse=True,
        )
        asks = sorted(
            [OrderLevel(price=float(a["price"]), size=float(a["size"])) for a in raw_asks],
            key=lambda x: x.price,
        )

        with self._lock:
            book = self.get_or_create(token_id)
            book.bids = bids
            book.asks = asks
            book.last_update = time.time()

        logger.debug(
            "📖 Book actualizado — token=%s…%s, bid=%.3f, ask=%.3f, spread=%.1f¢",
            token_id[:8],
            token_id[-6:],
            book.best_bid,
            book.best_ask,
            book.spread,
        )

    def process_price_change(self, data: dict[str, Any]) -> None:
        """Procesa un evento 'price_change' del WebSocket."""
        changes = data.get("price_changes", [data])
        for change in changes:
            token_id = change.get("asset_id", "")
            if not token_id:
                continue
            with self._lock:
                book = self.get_or_create(token_id)
                book.last_update = time.time()

    def process_last_trade(self, data: dict[str, Any]) -> None:
        """Procesa un evento 'last_trade_price'."""
        token_id = data.get("asset_id", "")
        price = data.get("price")
        if token_id and price:
            with self._lock:
                book = self.get_or_create(token_id)
                book.last_trade_price = float(price)
                book.last_update = time.time()

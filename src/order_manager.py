"""
src/order_manager.py — Gestión de Órdenes (Order Manager).

Responsabilidades:
  - Crear órdenes GTC limit (Maker) con feeRateBps en la firma
  - Cancelar órdenes obsoletas
  - Lógica de replace: cancel + new en secuencia asíncrona
  - Estado local de órdenes vivas (reconciliable con el CLOB)
  - Rate-limiting y throttling
  - Paper trading mode (loguea sin enviar)

Integración con el Quoting Engine:
  El Quoting Engine genera QuotePairs (precios y tamaños deseados).
  El Order Manager compara con las órdenes vivas y decide:
    - Si no hay orden → crear
    - Si la orden existente difiere del quote → cancel + crear
    - Si la orden está OK → no hacer nada
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from src.quoting import Quote, QuotePair

logger = logging.getLogger("polybot.orders")


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

@dataclass
class OrderManagerConfig:
    """Parámetros del gestor de órdenes."""
    clob_host: str = "https://clob.polymarket.com"
    simulation_mode: bool = True

    # Rate limiting
    max_orders_per_second: float = 5.0
    min_interval_between_orders_ms: float = 200.0

    # Fee rate
    fee_rate_cache_ttl_sec: float = 10.0    # Refrescar fee rate cada 10s

    # Reconciliación
    reconcile_interval_sec: float = 30.0

    # Tolerancia: no recotizar si el precio cambió menos de esto
    price_tolerance: float = 0.005          # 0.5 centavos


# ---------------------------------------------------------------------------
# Estado de una orden viva
# ---------------------------------------------------------------------------

@dataclass
class LiveOrder:
    """Representa una orden que hemos enviado y creemos viva."""
    order_id: str
    token_id: str
    side: str                # "BUY" | "SELL"
    price: float
    size: float
    condition_id: str
    created_at: float = 0.0
    status: str = "LIVE"     # LIVE | CANCELLED | FILLED | PARTIAL
    filled_size: float = 0.0
    fee_rate_bps: int = 0

    @property
    def remaining_size(self) -> float:
        return max(0, self.size - self.filled_size)

    @property
    def is_live(self) -> bool:
        return self.status == "LIVE"

    @property
    def age_sec(self) -> float:
        return time.time() - self.created_at if self.created_at else 0


# ---------------------------------------------------------------------------
# Order Manager
# ---------------------------------------------------------------------------

class OrderManager:
    """
    Gestiona el ciclo de vida de las órdenes Maker.

    Flujo principal (llamado en el loop de trading):
        1. Recibe QuotePair del Quoting Engine
        2. Compara con órdenes vivas
        3. Cancela las que difieren
        4. Crea las nuevas
        5. Actualiza estado local

    En simulation_mode, loguea las acciones sin enviarlas al CLOB.
    """

    def __init__(
        self,
        cfg: OrderManagerConfig | None = None,
        clob_client: Any = None,
    ):
        self.cfg = cfg or OrderManagerConfig()
        self._clob = clob_client              # py-clob-client ClobClient autenticado
        self._http = httpx.AsyncClient(timeout=10.0)

        # Estado local de órdenes
        self._live_orders: dict[str, LiveOrder] = {}   # order_id → LiveOrder
        self._orders_by_market: dict[str, list[str]] = {}  # condition_id → [order_ids]

        # Cache de fee rates
        self._fee_rate_cache: dict[str, tuple[int, float]] = {}  # token_id → (bps, timestamp)

        # Throttling
        self._last_order_time: float = 0.0
        self._order_count_window: list[float] = []

        # Métricas
        self.metrics = {
            "orders_created": 0,
            "orders_cancelled": 0,
            "orders_replaced": 0,
            "fills_received": 0,
            "errors": 0,
        }

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # API pública: Sincronizar quotes con órdenes vivas
    # ------------------------------------------------------------------

    async def sync_quotes(self, pair: QuotePair) -> dict[str, Any]:
        """
        Sincroniza un QuotePair con las órdenes vivas del mercado.
        Es el método principal que el loop de trading llama cada ciclo.

        Returns:
            dict con acciones tomadas:
            {"created": [...], "cancelled": [...], "unchanged": [...]}
        """
        cid = pair.market.condition_id
        actions = {"created": [], "cancelled": [], "unchanged": []}

        if not pair.is_complete:
            # Si no hay quotes completas, cancelar todo lo que tengamos en ese mercado
            await self._cancel_all_for_market(cid)
            return actions

        # Obtener órdenes vivas para este mercado
        live_ids = self._orders_by_market.get(cid, [])
        live_bids = [self._live_orders[oid] for oid in live_ids
                     if oid in self._live_orders and self._live_orders[oid].is_live and self._live_orders[oid].side == "BUY"]
        live_asks = [self._live_orders[oid] for oid in live_ids
                     if oid in self._live_orders and self._live_orders[oid].is_live and self._live_orders[oid].side == "SELL"]

        # Sincronizar BID
        bid_action = await self._sync_side(pair.bid, live_bids, cid)
        actions[bid_action].append("bid")

        # Sincronizar ASK
        ask_action = await self._sync_side(pair.ask, live_asks, cid)
        actions[ask_action].append("ask")

        return actions

    async def _sync_side(
        self,
        quote: Quote | None,
        live_orders: list[LiveOrder],
        condition_id: str,
    ) -> str:
        """
        Sincroniza una quote (bid o ask) con las órdenes vivas de ese lado.
        Returns: "created" | "cancelled" | "unchanged"
        """
        if not quote:
            # Cancelar órdenes de este lado
            for order in live_orders:
                await self._cancel_order(order)
            return "cancelled" if live_orders else "unchanged"

        # ¿Hay alguna orden viva en este lado que ya sea buena?
        for order in live_orders:
            if self._order_matches_quote(order, quote):
                return "unchanged"

        # Cancelar las que no coinciden
        for order in live_orders:
            await self._cancel_order(order)

        # Crear la nueva
        await self._create_order(quote, condition_id)
        return "created"

    def _order_matches_quote(self, order: LiveOrder, quote: Quote) -> bool:
        """Comprueba si una orden viva ya refleja la quote deseada."""
        price_diff = abs(order.price - quote.price)
        if price_diff > self.cfg.price_tolerance:
            return False
        # Tolerancia de tamaño: 20%
        if order.remaining_size > 0:
            size_ratio = quote.size / order.remaining_size
            if size_ratio < 0.8 or size_ratio > 1.2:
                return False
        return True

    # ------------------------------------------------------------------
    # Crear orden
    # ------------------------------------------------------------------

    async def _create_order(self, quote: Quote, condition_id: str) -> str | None:
        """
        Crea una orden GTC limit.
        En modo simulación, solo loguea.
        Returns: order_id o None
        """
        # Throttling
        await self._throttle()

        # Obtener fee rate DINÁMICO (nunca hardcoded)
        fee_bps = await self._get_fee_rate(quote.token_id)

        if self.cfg.simulation_mode:
            # PAPER TRADING: loguear sin enviar
            fake_id = f"SIM-{int(time.time()*1000)}-{quote.side}"
            logger.info(
                "📝 [SIMULACIÓN] Orden creada — %s %s @ %.3f × %.0f sh | fee=%d bps | market=%s",
                quote.side,
                "YES" if quote.token_id else "?",
                quote.price,
                quote.size,
                fee_bps,
                condition_id[:12],
            )
            order = LiveOrder(
                order_id=fake_id,
                token_id=quote.token_id,
                side=quote.side,
                price=quote.price,
                size=quote.size,
                condition_id=condition_id,
                created_at=time.time(),
                fee_rate_bps=fee_bps,
            )
            self._register_order(order)
            self.metrics["orders_created"] += 1
            return fake_id

        # MODO LIVE: enviar al CLOB
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            side = BUY if quote.side == "BUY" else SELL

            order_args = OrderArgs(
                token_id=quote.token_id,
                price=quote.price,
                size=quote.size,
                side=side,
                fee_rate_bps=fee_bps,
            )

            signed = self._clob.create_order(order_args)
            resp = self._clob.post_order(signed, OrderType.GTC)

            order_id = resp.get("orderID", resp.get("order_id", ""))
            if order_id:
                order = LiveOrder(
                    order_id=order_id,
                    token_id=quote.token_id,
                    side=quote.side,
                    price=quote.price,
                    size=quote.size,
                    condition_id=condition_id,
                    created_at=time.time(),
                    fee_rate_bps=fee_bps,
                )
                self._register_order(order)
                self.metrics["orders_created"] += 1
                logger.info(
                    "✅ Orden creada — id=%s, %s @ %.3f × %.0f sh, fee=%d bps",
                    order_id[:16],
                    quote.side,
                    quote.price,
                    quote.size,
                    fee_bps,
                )
                return order_id
            else:
                logger.warning("⚠️  Respuesta sin order_id: %s", resp)
                self.metrics["errors"] += 1
                return None

        except Exception as exc:
            logger.error("❌ Error creando orden: %s", exc)
            self.metrics["errors"] += 1
            return None

    # ------------------------------------------------------------------
    # Cancelar orden
    # ------------------------------------------------------------------

    async def _cancel_order(self, order: LiveOrder) -> bool:
        """Cancela una orden viva."""
        if not order.is_live:
            return True

        if self.cfg.simulation_mode:
            logger.info(
                "📝 [SIMULACIÓN] Orden cancelada — id=%s, %s @ %.3f",
                order.order_id[:16],
                order.side,
                order.price,
            )
            order.status = "CANCELLED"
            self.metrics["orders_cancelled"] += 1
            return True

        try:
            resp = self._clob.cancel(order.order_id)
            order.status = "CANCELLED"
            self.metrics["orders_cancelled"] += 1
            logger.info("🗑️ Orden cancelada — id=%s", order.order_id[:16])
            return True
        except Exception as exc:
            logger.error("❌ Error cancelando %s: %s", order.order_id[:16], exc)
            self.metrics["errors"] += 1
            return False

    async def _cancel_all_for_market(self, condition_id: str) -> int:
        """Cancela todas las órdenes vivas de un mercado."""
        live_ids = self._orders_by_market.get(condition_id, [])
        cancelled = 0
        for oid in live_ids:
            order = self._live_orders.get(oid)
            if order and order.is_live:
                if await self._cancel_order(order):
                    cancelled += 1
        return cancelled

    async def cancel_all(self) -> int:
        """Kill switch: cancela TODAS las órdenes vivas."""
        cancelled = 0
        for order in list(self._live_orders.values()):
            if order.is_live:
                if await self._cancel_order(order):
                    cancelled += 1
        logger.warning("🛑 KILL SWITCH — %d órdenes canceladas", cancelled)
        return cancelled

    # ------------------------------------------------------------------
    # Fee Rate dinámico
    # ------------------------------------------------------------------

    async def _get_fee_rate(self, token_id: str) -> int:
        """
        Obtiene el fee rate dinámico para un token.
        NUNCA hardcodeado — siempre consulta la API (con cache de 10s).

        Returns: fee_rate_bps (int). 0 si el mercado no tiene taker fees.
        """
        # Cache
        cached = self._fee_rate_cache.get(token_id)
        if cached:
            bps, ts = cached
            if time.time() - ts < self.cfg.fee_rate_cache_ttl_sec:
                return bps

        # Consultar API
        try:
            resp = await self._http.get(
                f"{self.cfg.clob_host}/fee-rate",
                params={"tokenID": token_id},
            )
            if resp.status_code == 200:
                data = resp.json()
                bps = int(data.get("fee_rate_bps", data.get("feeRateBps", 0)) or 0)
                self._fee_rate_cache[token_id] = (bps, time.time())
                return bps
        except Exception as exc:
            logger.debug("⚠️  fee-rate error: %s", exc)

        # Fallback: devolver 0 (sin fees) si no podemos consultar
        # Esto es seguro porque 0 bps = sin fees = la orden se aceptará igual
        return 0

    # ------------------------------------------------------------------
    # Procesamiento de fills (desde WebSocket user channel)
    # ------------------------------------------------------------------

    def process_fill(self, data: dict[str, Any]) -> None:
        """
        Procesa un evento de fill del WebSocket user channel.
        Actualiza el estado local de la orden.
        """
        order_id = data.get("id", data.get("order_id", ""))
        if not order_id:
            # Buscar en maker_orders
            for mo in data.get("maker_orders", []):
                mo_id = mo.get("order_id", "")
                if mo_id in self._live_orders:
                    order = self._live_orders[mo_id]
                    filled = float(mo.get("matched_amount", 0))
                    order.filled_size += filled
                    if order.remaining_size <= 0:
                        order.status = "FILLED"
                    else:
                        order.status = "PARTIAL"
                    self.metrics["fills_received"] += 1
                    logger.info(
                        "💰 Fill — id=%s, filled=%.1f, remaining=%.1f, status=%s",
                        mo_id[:16],
                        filled,
                        order.remaining_size,
                        order.status,
                    )
            return

        if order_id in self._live_orders:
            order = self._live_orders[order_id]
            filled = float(data.get("size", data.get("matched_amount", 0)))
            order.filled_size += filled
            status = data.get("status", "")
            if status == "MATCHED" or order.remaining_size <= 0:
                order.status = "FILLED"
            self.metrics["fills_received"] += 1

    def process_order_update(self, data: dict[str, Any]) -> None:
        """
        Procesa un evento de orden del WebSocket user channel.
        """
        order_id = data.get("id", "")
        event_type = data.get("type", "")

        if order_id in self._live_orders:
            order = self._live_orders[order_id]
            if event_type == "CANCELLATION":
                order.status = "CANCELLED"
            elif event_type == "PLACEMENT":
                order.status = "LIVE"

    # ------------------------------------------------------------------
    # Estado y métricas
    # ------------------------------------------------------------------

    def get_live_orders_for_market(self, condition_id: str) -> list[LiveOrder]:
        """Devuelve las órdenes vivas de un mercado."""
        ids = self._orders_by_market.get(condition_id, [])
        return [self._live_orders[oid] for oid in ids
                if oid in self._live_orders and self._live_orders[oid].is_live]

    def get_inventory(self, condition_id: str) -> tuple[float, float]:
        """
        Estima el inventario neto por fills.
        Returns: (yes_inventory, no_inventory)
        """
        yes_inv = 0.0
        no_inv = 0.0
        ids = self._orders_by_market.get(condition_id, [])
        for oid in ids:
            order = self._live_orders.get(oid)
            if order and order.filled_size > 0:
                if order.side == "BUY":
                    yes_inv += order.filled_size
                elif order.side == "SELL":
                    yes_inv -= order.filled_size  # Vendimos YES
        return yes_inv, no_inv

    def _register_order(self, order: LiveOrder) -> None:
        """Registra una orden en el estado local."""
        self._live_orders[order.order_id] = order
        if order.condition_id not in self._orders_by_market:
            self._orders_by_market[order.condition_id] = []
        self._orders_by_market[order.condition_id].append(order.order_id)

    # ------------------------------------------------------------------
    # Throttling
    # ------------------------------------------------------------------

    async def _throttle(self) -> None:
        """Respeta el rate limit de órdenes."""
        now = time.time()
        min_interval = self.cfg.min_interval_between_orders_ms / 1000.0
        elapsed = now - self._last_order_time
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)
        self._last_order_time = time.time()

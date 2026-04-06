"""
src/trading_loop.py — Bucle Principal de Trading.

Orquesta el ciclo continuo de market making:
  1. Para cada mercado activo:
     a. Comprobar si hay que recotizar (midpoint se movió, quote vieja, etc.)
     b. Generar nuevas quotes con el Quoting Engine
     c. Sincronizar quotes con órdenes vivas via Order Manager
  2. Procesar fills y actualizar inventario
  3. Repetir cada ciclo (objetivo: <100ms por ciclo)

Este módulo NO conoce los detalles de la API — delega en:
  - QuotingEngine para precios/tamaños
  - OrderManager para crear/cancelar órdenes
  - OrderbookTracker para el estado del libro
"""

import asyncio
import logging
import time
from typing import Any

from src.discovery import MarketCandidate
from src.orderbook import OrderbookTracker
from src.quoting import QuotingEngine, QuotingConfig
from src.order_manager import OrderManager, OrderManagerConfig
from src.risk_manager import RiskManager

logger = logging.getLogger("polybot.loop")


class TradingLoop:
    """
    Bucle de trading que ejecuta la estrategia Maker.

    Uso:
        loop = TradingLoop(markets, orderbook, clob_client, simulation=True)
        await loop.run()  # Corre indefinidamente
    """

    def __init__(
        self,
        markets: list[MarketCandidate],
        orderbook: OrderbookTracker,
        clob_client: Any = None,
        simulation: bool = True,
        bankroll_usd: float = 1000.0,
        cycle_interval_ms: float = 500,   # Intervalo entre ciclos en ms
        risk_manager: RiskManager | None = None,
    ):
        self._markets = markets
        self._orderbook = orderbook
        self._simulation = simulation
        self._bankroll = bankroll_usd
        self._cycle_interval = cycle_interval_ms / 1000.0
        self._running = False
        self._cycle_count = 0
        self._risk = risk_manager  # None en modo legacy (sin gestión de riesgo)

        # Componentes
        self._quoting = QuotingEngine(
            cfg=QuotingConfig(),
            orderbook=orderbook,
        )
        self._orders = OrderManager(
            cfg=OrderManagerConfig(
                simulation_mode=simulation,
            ),
            clob_client=clob_client,
        )

    # ------------------------------------------------------------------
    # Bucle principal
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Ejecuta el bucle de trading indefinidamente."""
        self._running = True
        logger.info(
            "🔄 Trading loop iniciado — %d mercados, interval=%dms, simulation=%s",
            len(self._markets),
            int(self._cycle_interval * 1000),
            self._simulation,
        )

        try:
            while self._running:
                cycle_start = time.monotonic()

                await self._run_cycle()

                # Medir duración del ciclo
                cycle_ms = (time.monotonic() - cycle_start) * 1000
                self._cycle_count += 1

                if self._cycle_count % 100 == 0:
                    logger.info(
                        "📊 Ciclo #%d — %.1fms | created=%d, cancelled=%d, fills=%d, errors=%d",
                        self._cycle_count,
                        cycle_ms,
                        self._orders.metrics["orders_created"],
                        self._orders.metrics["orders_cancelled"],
                        self._orders.metrics["fills_received"],
                        self._orders.metrics["errors"],
                    )
                    if self._risk is not None:
                        self._risk.log_stats()

                # Esperar hasta el siguiente ciclo
                elapsed = time.monotonic() - cycle_start
                sleep_time = max(0, self._cycle_interval - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            logger.info("🛑 Trading loop cancelado")
        except Exception:
            logger.exception("💥 Error en trading loop")
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        """Detiene el bucle limpiamente."""
        self._running = False

    # ------------------------------------------------------------------
    # Un ciclo de trading
    # ------------------------------------------------------------------

    async def _run_cycle(self) -> None:
        """Ejecuta un ciclo completo de market making."""
        # Comprobar kill switch antes de procesar cualquier mercado
        if self._risk is not None:
            triggered, reason = self._risk.check_kill_switch()
            if triggered:
                logger.critical("🚨 KILL SWITCH activo (%s) — deteniendo el bot", reason)
                await self._orders.cancel_all()
                self._running = False
                return

        for market in self._markets:
            try:
                await self._process_market(market)
                if self._risk is not None:
                    self._risk.record_success()
            except Exception:
                logger.exception(
                    "❌ Error procesando mercado %s", market.question[:40]
                )
                if self._risk is not None:
                    self._risk.record_error()

    async def _process_market(self, market: MarketCandidate) -> None:
        """Procesa un mercado individual en un ciclo."""

        # ¿Necesitamos recotizar?
        should, reason = self._quoting.should_requote(market)

        if not should:
            return  # Todo sigue OK, no hacer nada

        # Obtener inventario actual
        inv_yes, inv_no = self._orders.get_inventory(market.condition_id)

        # Generar nuevas quotes (con Kelly sizing si hay risk_manager)
        pair = self._quoting.generate_quotes(
            market=market,
            inventory_yes=inv_yes,
            inventory_no=inv_no,
            bankroll_usd=self._bankroll,
            risk_manager=self._risk,
        )

        if not pair.is_complete:
            logger.debug(
                "⏭️ No se pudo generar quote completa para %s",
                market.question[:40],
            )
            return

        # Sincronizar con órdenes vivas (cancel obsoletas + crear nuevas)
        actions = await self._orders.sync_quotes(pair)

        if actions["created"] or actions["cancelled"]:
            logger.debug(
                "🔄 Sync — %s | reason=%s | created=%s, cancelled=%s",
                market.question[:30],
                reason,
                actions["created"],
                actions["cancelled"],
            )

    # ------------------------------------------------------------------
    # Procesamiento de eventos del WebSocket user channel
    # ------------------------------------------------------------------

    def on_user_event(self, data: dict[str, Any]) -> None:
        """
        Procesa eventos del canal user del WebSocket.
        Llamado desde main.py cuando llega un evento.
        """
        event_type = data.get("event_type", "")

        if event_type == "trade":
            self._orders.process_fill(data)
            # Registrar fill en el risk manager para actualizar P&L y posición
            if self._risk is not None:
                cid = data.get("market", data.get("condition_id", ""))
                side = data.get("side", "")
                price = float(data.get("price", 0))
                size = float(data.get("size", data.get("matched_amount", 0)))
                if cid and side and price and size:
                    self._risk.record_fill(cid, side.upper(), price, size)
        elif event_type == "order":
            self._orders.process_order_update(data)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def _shutdown(self) -> None:
        """Cierre limpio: cancela todas las órdenes vivas."""
        logger.info("🛑 Cerrando trading loop — cancelando órdenes vivas...")
        cancelled = await self._orders.cancel_all()
        logger.info("✅ %d órdenes canceladas. Trading loop cerrado.", cancelled)
        await self._orders.close()

    # ------------------------------------------------------------------
    # Control externo
    # ------------------------------------------------------------------

    def update_markets(self, markets: list[MarketCandidate]) -> None:
        """Actualiza la lista de mercados activos (desde re-scan del discovery)."""
        old_cids = {m.condition_id for m in self._markets}
        new_cids = {m.condition_id for m in markets}

        added = new_cids - old_cids
        removed = old_cids - new_cids

        if added:
            logger.info("➕ Mercados añadidos: %d", len(added))
        if removed:
            logger.info("➖ Mercados retirados: %d", len(removed))

        self._markets = markets

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def cycle_count(self) -> int:
        return self._cycle_count

    @property
    def order_metrics(self) -> dict:
        return self._orders.metrics

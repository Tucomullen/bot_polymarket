"""
src/arb_loop.py — Bucle de arbitraje YES+NO.

Escanea periódicamente mercados buscando arb, loguea oportunidades,
y en modo live las ejecuta comprando YES y NO simultáneamente.

Gestión de riesgo integrada:
  - Budget limitado por PortfolioAllocator (STRATEGY_ARB)
  - Kill switch del RiskManager compartido
  - Tamaño por posición acotado (min/max configurable)
"""

import asyncio
import logging
from typing import Any

from src.arb_scanner import ArbScanner, ArbOpportunity, ArbScannerConfig
from src.portfolio import PortfolioAllocator, STRATEGY_ARB

logger = logging.getLogger("polybot.arb")

_SCAN_INTERVAL_SEC = 60.0
_MIN_POSITION_USD = 20.0   # Por cada lado (YES y NO)
_MAX_POSITION_USD = 150.0  # Por cada lado


class ArbLoop:
    """
    Bucle de arbitraje YES+NO.

    Uso:
        loop = ArbLoop(portfolio, clob_client, simulation=True)
        await loop.run()
    """

    def __init__(
        self,
        portfolio: PortfolioAllocator,
        clob_client: Any = None,
        simulation: bool = True,
        scan_interval_sec: float = _SCAN_INTERVAL_SEC,
        min_position_usd: float = _MIN_POSITION_USD,
        max_position_usd: float = _MAX_POSITION_USD,
    ):
        self._portfolio = portfolio
        self._clob = clob_client
        self._simulation = simulation
        self._interval = scan_interval_sec
        self._min_pos = min_position_usd
        self._max_pos = max_position_usd
        self._scanner = ArbScanner(ArbScannerConfig())
        self._running = False
        self._open_positions: dict[str, ArbOpportunity] = {}  # condition_id → opp
        self._total_deployed: float = 0.0   # USD total en posiciones abiertas
        self._cycle: int = 0

    async def run(self) -> None:
        self._running = True
        logger.info(
            "💹 Arb loop iniciado — interval=%ds, simulation=%s, budget=$%.0f",
            int(self._interval),
            self._simulation,
            self._portfolio.get_budget(STRATEGY_ARB),
        )
        try:
            while self._running:
                self._cycle += 1
                await self._run_cycle()
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            logger.info("🛑 Arb loop cancelado")
        finally:
            await self._scanner.close()

    async def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Ciclo principal
    # ------------------------------------------------------------------

    async def _run_cycle(self) -> None:
        # Kill switch — el mismo que comparte el RiskManager
        rm = self._portfolio.risk_manager
        triggered, reason = rm.check_kill_switch()
        if triggered:
            logger.critical("🚨 KILL SWITCH activo (%s) — arb loop detenido", reason)
            self._running = False
            return

        try:
            opportunities = await self._scanner.scan()
        except Exception:
            logger.exception("❌ Error en arb scan")
            return

        if not opportunities:
            logger.info(
                "💹 Arb scan #%d: sin oportunidades (gap < %.0f¢)",
                self._cycle, self._scanner.cfg.min_gap * 100,
            )
            return

        logger.info("💹 Arb scan #%d: %d oportunidades encontradas", self._cycle, len(opportunities))
        for opp in opportunities:
            self._log_opportunity(opp)
            await self._maybe_enter(opp)

    def _log_opportunity(self, opp: ArbOpportunity) -> None:
        logger.info(
            "   💰 ARB: %s | YES_ask=%.3f + NO_ask=%.3f = %.3f | gap=%.2f¢ (%.1f%%) | %.0fh",
            opp.question[:55],
            opp.yes_ask, opp.no_ask, opp.cost_per_share,
            opp.gap * 100, opp.gap_pct,
            opp.hours_to_resolution,
        )

    async def _maybe_enter(self, opp: ArbOpportunity) -> None:
        if opp.condition_id in self._open_positions:
            return

        available = self._portfolio.get_available(STRATEGY_ARB)
        # Tamaño: 10% del budget disponible, acotado por min/max
        size_each = min(self._max_pos, max(self._min_pos, available * 0.10))

        # Coste total = size_each / yes_ask shares × yes_ask  +  size_each / no_ask × no_ask
        # = size_each × 2 (una unidad por lado)
        total_cost = size_each * 2
        if total_cost > available:
            logger.info(
                "   ⚠️  Arb: sin budget (disponible=$%.2f, necesario=$%.2f)",
                available, total_cost,
            )
            return

        if self._simulation:
            logger.info(
                "   [SIM] Entraría arb %s — $%.0f×2 (YES@%.3f + NO@%.3f) → beneficio $%.2f",
                opp.condition_id[:12],
                size_each, opp.yes_ask, opp.no_ask,
                size_each / opp.cost_per_share * opp.gap * 2,
            )
            # Registrar la posición para no volver a logearla en el siguiente scan
            self._open_positions[opp.condition_id] = opp
            self._portfolio.record_order_opened(STRATEGY_ARB, opp.condition_id, total_cost)
        else:
            await self._execute(opp, size_each)

    # ------------------------------------------------------------------
    # Ejecución real (live mode)
    # ------------------------------------------------------------------

    async def _execute(self, opp: ArbOpportunity, size_usd_each: float) -> None:
        """
        Compra YES y NO en el CLOB como órdenes taker (GTC al best ask).
        El orden importa: primero YES, luego NO inmediatamente.
        Si el leg YES falla, abortamos sin tocar NO.
        """
        if self._clob is None:
            logger.error("❌ Arb execute: no hay clob_client configurado")
            return

        cid = opp.condition_id
        shares_yes = round(size_usd_each / opp.yes_ask, 2)
        shares_no = round(size_usd_each / opp.no_ask, 2)

        logger.info(
            "⚡ Ejecutando arb %s | YES: %s sh @ %.3f | NO: %s sh @ %.3f",
            cid[:12], shares_yes, opp.yes_ask, shares_no, opp.no_ask,
        )

        try:
            # Leg 1: comprar YES
            order_yes = self._clob.create_order({
                "token_id": opp.token_id_yes,
                "side": "BUY",
                "price": opp.yes_ask,
                "size": shares_yes,
                "order_type": "FOK",  # Fill-or-Kill para evitar ejecución parcial
            })
            if not order_yes:
                logger.warning("   ⚠️  Leg YES no ejecutado — abortando arb")
                return

            # Leg 2: comprar NO
            order_no = self._clob.create_order({
                "token_id": opp.token_id_no,
                "side": "BUY",
                "price": opp.no_ask,
                "size": shares_no,
                "order_type": "FOK",
            })
            if not order_no:
                logger.warning("   ⚠️  Leg NO no ejecutado — posición YES abierta sin hedge!")
                # En producción: aquí habría que manejar el riesgo de tener solo un leg

            self._open_positions[cid] = opp
            self._portfolio.record_order_opened(STRATEGY_ARB, cid, size_usd_each * 2)
            logger.info("   ✅ Arb abierto — beneficio esperado: $%.2f",
                        shares_yes * opp.gap)

        except Exception:
            logger.exception("❌ Error ejecutando arb %s", cid[:12])

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @property
    def open_count(self) -> int:
        return len(self._open_positions)

    def get_stats(self) -> dict:
        return {
            "cycle": self._cycle,
            "open_positions": self.open_count,
            "budget_usd": self._portfolio.get_budget(STRATEGY_ARB),
            "available_usd": self._portfolio.get_available(STRATEGY_ARB),
        }

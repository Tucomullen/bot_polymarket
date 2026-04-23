"""
src/portfolio.py — Capa de asignación de capital entre estrategias.

Distribuye el bankroll total entre estrategias activas y las une bajo un
único RiskManager. Cada estrategia usa su budget asignado para Kelly sizing,
pero el kill switch mira el P&L combinado del bankroll total.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.risk_manager import RiskManager

logger = logging.getLogger("polybot.portfolio")

# Identificadores canónicos de estrategia
STRATEGY_MM = "mm"
STRATEGY_BTC_SNIPER = "btc_sniper"
STRATEGY_ARB = "arb"
STRATEGY_SMART_MONEY = "smart_money"

# Asignación por defecto — ajustable vía .env en el futuro
DEFAULT_ALLOCATIONS: dict[str, float] = {
    STRATEGY_MM: 0.60,           # Market making: 60%
    STRATEGY_ARB: 0.20,          # Arb YES+NO: 20%
    STRATEGY_SMART_MONEY: 0.20,  # Smart Money copying: 20%
}


@dataclass
class StrategySlot:
    strategy_id: str
    budget_usd: float
    open_exposure_usd: float = 0.0

    @property
    def available_usd(self) -> float:
        return max(0.0, self.budget_usd - self.open_exposure_usd)


class PortfolioAllocator:
    """
    Distribuye bankroll entre estrategias y centraliza el tracking de riesgo.

    Kelly sizing: usa el budget de cada estrategia (más pequeño que el bankroll total).
    Kill switch:  el RiskManager mira el P&L combinado del bankroll total.

    Uso:
        alloc = PortfolioAllocator(1000, {"mm": 0.40, "arb": 0.20, ...}, rm)
        budget = alloc.get_budget("mm")        # → 400.0
        avail  = alloc.get_available("mm")     # → 400.0 − exposición abierta
        alloc.record_order_opened("mm", cid, 50.0)
    """

    def __init__(
        self,
        bankroll: float,
        allocations: dict[str, float],
        risk_manager: "RiskManager",
    ):
        total = sum(allocations.values())
        if abs(total - 1.0) > 0.01:
            raise ValueError(
                f"Las allocations deben sumar 1.0, obtenido {total:.3f}"
            )

        self._bankroll = bankroll
        self._risk = risk_manager
        self._slots: dict[str, StrategySlot] = {
            sid: StrategySlot(
                strategy_id=sid,
                budget_usd=round(bankroll * pct, 2),
            )
            for sid, pct in allocations.items()
        }

        logger.info(
            "PortfolioAllocator — bankroll=$%.0f | %s",
            bankroll,
            " | ".join(
                f"{sid}=${s.budget_usd:.0f} ({pct*100:.0f}%%)"
                for (sid, pct), s in zip(allocations.items(), self._slots.values())
            ),
        )

    # ── Consulta ─────────────────────────────────────────────────────────

    def get_budget(self, strategy_id: str) -> float:
        """Budget máximo asignado a esta estrategia."""
        s = self._slots.get(strategy_id)
        return s.budget_usd if s else 0.0

    def get_available(self, strategy_id: str) -> float:
        """Budget disponible descontando exposición abierta actual."""
        s = self._slots.get(strategy_id)
        return s.available_usd if s else 0.0

    # ── Registro de órdenes ──────────────────────────────────────────────

    def record_order_opened(self, strategy_id: str, condition_id: str, size_usd: float) -> None:
        s = self._slots.get(strategy_id)
        if s:
            s.open_exposure_usd += size_usd
        self._risk.record_order_opened(condition_id, size_usd)

    def record_order_closed(self, strategy_id: str, condition_id: str, size_usd: float) -> None:
        s = self._slots.get(strategy_id)
        if s:
            s.open_exposure_usd = max(0.0, s.open_exposure_usd - size_usd)
        self._risk.record_order_closed(condition_id, size_usd)

    def record_fill(
        self,
        _strategy_id: str,
        condition_id: str,
        side: str,
        price: float,
        size: float,
    ) -> None:
        """Registra fill en el RiskManager global (P&L combinado para kill switch)."""
        self._risk.record_fill(condition_id, side, price, size)

    # ── Acceso al RiskManager central ────────────────────────────────────

    @property
    def risk_manager(self) -> "RiskManager":
        return self._risk

    # ── Stats ─────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        return {
            "bankroll": self._bankroll,
            "strategies": {
                sid: {
                    "budget": s.budget_usd,
                    "exposure": round(s.open_exposure_usd, 2),
                    "available": round(s.available_usd, 2),
                }
                for sid, s in self._slots.items()
            },
        }

    def log_stats(self) -> None:
        for sid, s in self._slots.items():
            logger.info(
                "Portfolio [%s] budget=$%.0f | exposure=$%.2f | available=$%.2f",
                sid, s.budget_usd, s.open_exposure_usd, s.available_usd,
            )

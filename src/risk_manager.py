"""
src/risk_manager.py — Gestión de Riesgo (Risk Manager).

Centraliza toda la lógica de riesgo del bot:
  - Kill switch: para de emergencia ante pérdida excesiva o errores
  - Exposición: rastrea capital comprometido en órdenes vivas por mercado y total
  - Kelly sizing: calcula el tamaño óptimo de orden según el edge del spread
  - P&L tracking: monitoriza ganancias/pérdidas de la sesión

Fórmula Kelly para market making:
  edge = half_spread_cents / 100 / mid_price   (fracción del precio que capturamos)
  kelly_size = bankroll * kelly_fraction * edge * 2
  Limitado siempre por max_order_risk_pct y la capacidad restante de exposición total.
"""

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("polybot.risk")


# ---------------------------------------------------------------------------
# Exposición por mercado
# ---------------------------------------------------------------------------

@dataclass
class MarketExposure:
    """Rastrea el capital comprometido y P&L de un mercado individual."""
    condition_id: str
    live_usd: float = 0.0        # Capital en órdenes vivas (aún no llenadas)
    filled_yes: float = 0.0      # Shares YES acumuladas (posición larga neta)
    avg_entry_price: float = 0.0 # Precio medio de entrada de la posición larga
    realized_pnl: float = 0.0    # P&L realizado (spread capturado en fills)


# ---------------------------------------------------------------------------
# Risk Manager
# ---------------------------------------------------------------------------

class RiskManager:
    """
    Controla el riesgo global del bot.

    Uso:
        rm = RiskManager(bankroll_usd=1000, kelly_fraction=0.25, ...)
        size = rm.max_order_size_usd(cid, half_spread_cents=1.0, mid=0.50)
        triggered, reason = rm.check_kill_switch()
    """

    def __init__(
        self,
        bankroll_usd: float = 1000.0,
        kelly_fraction: float = 0.25,
        max_order_risk_pct: float = 0.05,      # Máx % del bankroll por orden individual
        max_total_exposure_pct: float = 0.20,  # Máx % del bankroll en órdenes vivas
        max_session_loss_pct: float = 0.05,    # Kill switch al superar esta pérdida
        max_consecutive_errors: int = 10,      # Kill switch si hay X errores seguidos
    ):
        self._bankroll = bankroll_usd
        self._kelly = kelly_fraction
        self._max_order_pct = max_order_risk_pct
        self._max_total_pct = max_total_exposure_pct
        self._max_loss_pct = max_session_loss_pct
        self._max_errors = max_consecutive_errors

        self._exposures: dict[str, MarketExposure] = {}
        self._session_pnl: float = 0.0
        self._consecutive_errors: int = 0
        self._kill_triggered: bool = False
        self._kill_reason: str = ""
        self._started_at: float = time.time()

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    def check_kill_switch(self) -> tuple[bool, str]:
        """
        Comprueba si deben detenerse las operaciones.
        Returns: (triggered, reason)
        """
        if self._kill_triggered:
            return True, self._kill_reason

        # Condición 1: pérdida de sesión supera el límite
        max_loss = self._bankroll * self._max_loss_pct
        if self._session_pnl < -max_loss:
            self._trigger("pérdida de sesión ${:.2f} supera límite ${:.2f}".format(
                abs(self._session_pnl), max_loss
            ))
            return True, self._kill_reason

        # Condición 2: demasiados errores consecutivos de API
        if self._consecutive_errors >= self._max_errors:
            self._trigger(f"{self._consecutive_errors} errores consecutivos de API")
            return True, self._kill_reason

        return False, ""

    def _trigger(self, reason: str) -> None:
        self._kill_triggered = True
        self._kill_reason = reason
        logger.critical("🚨 KILL SWITCH activado: %s", reason)

    def reset_kill_switch(self) -> None:
        """Resetea el kill switch. Solo usar tras revisión manual del problema."""
        self._kill_triggered = False
        self._kill_reason = ""
        self._consecutive_errors = 0
        logger.warning("⚠️  Kill switch reseteado manualmente")

    # ------------------------------------------------------------------
    # Tracking de errores
    # ------------------------------------------------------------------

    def record_error(self) -> None:
        self._consecutive_errors += 1

    def record_success(self) -> None:
        if self._consecutive_errors > 0:
            self._consecutive_errors = 0

    # ------------------------------------------------------------------
    # Exposición: registro de órdenes abiertas/cerradas
    # ------------------------------------------------------------------

    def record_order_opened(self, condition_id: str, size_usd: float) -> None:
        """Llama cuando se crea una nueva orden live."""
        self._get_exp(condition_id).live_usd += size_usd

    def record_order_closed(self, condition_id: str, size_usd: float) -> None:
        """Llama cuando se cancela una orden sin llenar."""
        exp = self._get_exp(condition_id)
        exp.live_usd = max(0.0, exp.live_usd - size_usd)

    def record_fill(
        self, condition_id: str, side: str, price: float, size: float
    ) -> None:
        """
        Registra un fill del WebSocket.
        - BUY fill → acumula posición larga, reduce exposición live
        - SELL fill → realiza P&L (spread capturado), reduce posición
        """
        exp = self._get_exp(condition_id)
        usd = price * size

        if side == "BUY":
            # Precio medio ponderado de entrada
            total_cost = exp.avg_entry_price * exp.filled_yes + usd
            exp.filled_yes += size
            exp.avg_entry_price = total_cost / exp.filled_yes if exp.filled_yes > 0 else 0.0
            exp.live_usd = max(0.0, exp.live_usd - usd)

        elif side == "SELL":
            # Capturamos el spread si teníamos posición larga
            if exp.filled_yes > 0 and exp.avg_entry_price > 0:
                filled_portion = min(size, exp.filled_yes)
                pnl = (price - exp.avg_entry_price) * filled_portion
                exp.realized_pnl += pnl
                self._session_pnl += pnl
            exp.filled_yes = max(0.0, exp.filled_yes - size)
            exp.live_usd = max(0.0, exp.live_usd - usd)

    def get_market_exposure(self, condition_id: str) -> float:
        """USD en órdenes vivas para un mercado concreto."""
        return self._exposures.get(condition_id, MarketExposure(condition_id)).live_usd

    def get_total_exposure(self) -> float:
        """USD total en órdenes vivas en todos los mercados."""
        return sum(e.live_usd for e in self._exposures.values())

    # ------------------------------------------------------------------
    # Kelly sizing
    # ------------------------------------------------------------------

    def max_order_size_usd(
        self,
        condition_id: str,
        half_spread_cents: float,
        mid_price: float,
    ) -> float:
        """
        Tamaño máximo de orden (USD) según Kelly fraccional.

        Kelly para market making:
          edge = (half_spread_cents / 100) / mid_price
          kelly_size = bankroll * kelly_fraction * edge * 2

        Limitado por:
          - max_order_risk_pct * bankroll (hard cap por orden)
          - capacidad restante hasta max_total_exposure_pct * bankroll
        """
        # Kelly sizing
        if mid_price > 0 and half_spread_cents > 0:
            edge = (half_spread_cents / 100.0) / mid_price
            kelly_size = self._bankroll * self._kelly * edge * 2.0
        else:
            kelly_size = self._bankroll * self._max_order_pct

        # Hard cap por orden individual
        max_per_order = self._bankroll * self._max_order_pct
        size = min(kelly_size, max_per_order)

        # Respetar límite de exposición total
        total_exp = self.get_total_exposure()
        max_total = self._bankroll * self._max_total_pct
        remaining = max(0.0, max_total - total_exp)
        # Dividir entre 2 porque cada ciclo abre bid+ask simultáneamente
        size = min(size, remaining / 2.0)

        return max(0.0, size)

    # ------------------------------------------------------------------
    # Stats y reporting
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """Devuelve un resumen del estado de riesgo para logging."""
        total_exp = self.get_total_exposure()
        uptime_min = (time.time() - self._started_at) / 60.0
        return {
            "session_pnl_usd": round(self._session_pnl, 4),
            "total_exposure_usd": round(total_exp, 2),
            "total_exposure_pct": round(total_exp / self._bankroll * 100, 1) if self._bankroll else 0,
            "consecutive_errors": self._consecutive_errors,
            "kill_switch": self._kill_triggered,
            "uptime_min": round(uptime_min, 1),
        }

    def log_stats(self) -> None:
        s = self.get_stats()
        logger.info(
            "📊 Riesgo — PnL=%.4f | Exposición=%.1f%% ($%.2f) | Errores=%d | KS=%s | Uptime=%.1fmin",
            s["session_pnl_usd"],
            s["total_exposure_pct"],
            s["total_exposure_usd"],
            s["consecutive_errors"],
            "🚨" if s["kill_switch"] else "✅",
            s["uptime_min"],
        )

    def _get_exp(self, condition_id: str) -> MarketExposure:
        if condition_id not in self._exposures:
            self._exposures[condition_id] = MarketExposure(condition_id=condition_id)
        return self._exposures[condition_id]

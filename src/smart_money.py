"""
src/smart_money.py — Tracker de Smart Money (Phase C).

Monitoriza wallets de traders con historial de aciertos en Polymarket.
Cuando abren una nueva posición relevante, la copiamos con tamaño fijo.

Flujo:
  1. Cada POLL_INTERVAL segundos: consulta posiciones actuales de cada wallet
  2. Compara con snapshot anterior → detecta nuevas posiciones
  3. Para cada posición nueva: copia con tamaño proporcional al budget STRATEGY_SMART_MONEY
  4. Risk manager compartido → kill switch aplica también aquí

API usada (pública, sin auth):
  GET https://data-api.polymarket.com/positions?user=PROXY_WALLET
  GET https://data-api.polymarket.com/trades?userAddress=WALLET&limit=N
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field

import httpx

from src.portfolio import PortfolioAllocator, STRATEGY_SMART_MONEY

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

logger = logging.getLogger("polybot.smartmoney")

_POLL_INTERVAL_SEC = 300.0   # Cada 5 minutos
_MIN_WALLET_POSITION_USD = 50.0   # Ignorar posiciones pequeñas del wallet trackeado
_MIN_HOURS_TO_RESOLVE = 12.0      # No copiar si resuelve en < 12h
_COPY_FRACTION = 0.15             # Copiar 15% de su tamaño, acotado por max
_MAX_COPY_USD = 100.0


@dataclass
class TrackedPosition:
    """Una posición de un wallet trackeado."""
    proxy_wallet: str
    asset: str              # token_id
    condition_id: str
    title: str
    size: float             # shares
    avg_price: float
    current_value_usd: float
    outcome: str = ""       # "Yes" / "No" / outcome name (del CLOB)


@dataclass
class SmartMoneyConfig:
    """Wallets a trackear (proxy wallet addresses de Polymarket)."""
    wallets: list[str] = field(default_factory=list)
    poll_interval_sec: float = _POLL_INTERVAL_SEC
    min_wallet_position_usd: float = _MIN_WALLET_POSITION_USD
    min_hours_to_resolve: float = _MIN_HOURS_TO_RESOLVE
    copy_fraction: float = _COPY_FRACTION
    max_copy_usd: float = _MAX_COPY_USD

    @classmethod
    def from_env(cls) -> "SmartMoneyConfig":
        """Lee wallets desde SMART_MONEY_WALLETS (comma-separated) en .env."""
        raw = os.getenv("SMART_MONEY_WALLETS", "").strip()
        wallets = [w.strip().lower() for w in raw.split(",") if w.strip()]
        return cls(wallets=wallets)


class SmartMoneyLoop:
    """
    Bucle de seguimiento de Smart Money.

    Uso:
        cfg = SmartMoneyConfig.from_env()
        loop = SmartMoneyLoop(cfg, portfolio, clob_client, simulation=True)
        await loop.run()
    """

    def __init__(
        self,
        cfg: SmartMoneyConfig,
        portfolio: PortfolioAllocator,
        clob_client=None,
        simulation: bool = True,
    ):
        self._cfg = cfg
        self._portfolio = portfolio
        self._clob = clob_client
        self._simulation = simulation
        self._running = False
        self._client: httpx.AsyncClient | None = None
        self._cycle = 0

        # Snapshots anteriores: proxy_wallet → {condition_id → TrackedPosition}
        self._snapshots: dict[str, dict[str, TrackedPosition]] = {}

        # Posiciones que ya copiamos (no volver a copiar)
        self._copied: set[str] = set()  # "proxy_wallet:condition_id"

    async def run(self) -> None:
        if not self._cfg.wallets:
            logger.warning(
                "⚠️  Smart Money: sin wallets configuradas. "
                "Añade proxy wallet addresses en SMART_MONEY_WALLETS en .env"
            )
            return

        self._client = httpx.AsyncClient(timeout=15.0)
        self._running = True
        logger.info(
            "🧠 Smart Money loop iniciado — %d wallets, interval=%ds, simulation=%s",
            len(self._cfg.wallets),
            int(self._cfg.poll_interval_sec),
            self._simulation,
        )

        try:
            while self._running:
                self._cycle += 1
                await self._poll_all()
                await asyncio.sleep(self._cfg.poll_interval_sec)
        except asyncio.CancelledError:
            logger.info("🛑 Smart Money loop cancelado")
        finally:
            if self._client:
                await self._client.aclose()

    async def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Ciclo principal
    # ------------------------------------------------------------------

    async def _poll_all(self) -> None:
        rm = self._portfolio.risk_manager
        triggered, reason = rm.check_kill_switch()
        if triggered:
            logger.critical("🚨 KILL SWITCH activo (%s) — smart money detenido", reason)
            self._running = False
            return

        logger.info("🧠 Smart Money poll #%d — %d wallets", self._cycle, len(self._cfg.wallets))

        for wallet in self._cfg.wallets:
            try:
                await self._poll_wallet(wallet)
            except Exception:
                logger.exception("❌ Error polling wallet %s", wallet[:10])
            await asyncio.sleep(1.0)

    async def _poll_wallet(self, proxy_wallet: str) -> None:
        positions = await self._fetch_positions(proxy_wallet)
        if positions is None:
            return

        prev = self._snapshots.get(proxy_wallet, {})
        new_positions = {
            cid: pos for cid, pos in positions.items()
            if cid not in prev
            and pos.current_value_usd >= self._cfg.min_wallet_position_usd
        }

        if new_positions:
            logger.info(
                "🧠 [%s...] %d posición(es) nueva(s) detectada(s)",
                proxy_wallet[:10], len(new_positions),
            )
            for cid, pos in new_positions.items():
                logger.info(
                    "   📌 %s | outcome=%s | size=%.0f sh @ %.3f ($%.0f)",
                    pos.title[:55], pos.outcome, pos.size,
                    pos.avg_price, pos.current_value_usd,
                )
                key = f"{proxy_wallet}:{cid}"
                if key not in self._copied:
                    await self._maybe_copy(pos)
                    self._copied.add(key)
        else:
            logger.debug("🧠 [%s...] sin cambios", proxy_wallet[:10])

        self._snapshots[proxy_wallet] = positions

    # ------------------------------------------------------------------
    # API: posiciones de un wallet
    # ------------------------------------------------------------------

    async def _fetch_positions(self, proxy_wallet: str) -> dict[str, TrackedPosition] | None:
        try:
            r = await self._client.get(
                f"{DATA_API}/positions",
                params={"user": proxy_wallet, "sizeThreshold": "1"},
            )
            if r.status_code != 200:
                logger.debug("positions HTTP %d for %s", r.status_code, proxy_wallet[:10])
                return None

            result: dict[str, TrackedPosition] = {}
            for item in r.json():
                cid = item.get("conditionId", "")
                if not cid:
                    continue
                pos = TrackedPosition(
                    proxy_wallet=proxy_wallet,
                    asset=item.get("asset", ""),
                    condition_id=cid,
                    title=item.get("title", ""),
                    size=float(item.get("size", 0)),
                    avg_price=float(item.get("avgPrice", 0)),
                    current_value_usd=float(item.get("currentValue", 0)),
                    outcome=item.get("outcome", ""),
                )
                result[cid] = pos
            return result

        except Exception:
            logger.exception("Error fetching positions for %s", proxy_wallet[:10])
            return None

    # ------------------------------------------------------------------
    # Copiar posición
    # ------------------------------------------------------------------

    async def _maybe_copy(self, pos: TrackedPosition) -> None:
        available = self._portfolio.get_available(STRATEGY_SMART_MONEY)
        size_usd = min(
            self._cfg.max_copy_usd,
            max(5.0, pos.current_value_usd * self._cfg.copy_fraction),
            available * 0.20,
        )

        if size_usd < 5.0:
            logger.info(
                "   ⚠️  SM: sin budget suficiente (disponible=$%.2f)", available
            )
            return

        if self._simulation:
            logger.info(
                "   [SIM] Copiaría: %s | outcome=%s | $%.0f (%.0f%% del suyo)",
                pos.title[:50], pos.outcome, size_usd,
                size_usd / pos.current_value_usd * 100 if pos.current_value_usd > 0 else 0,
            )
            self._portfolio.record_order_opened(
                STRATEGY_SMART_MONEY, pos.condition_id, size_usd
            )
        else:
            await self._execute_copy(pos, size_usd)

    async def _execute_copy(self, pos: TrackedPosition, size_usd: float) -> None:
        """Coloca una orden de compra en el CLOB copiando la posición."""
        if self._clob is None:
            logger.error("❌ SmartMoney execute: no hay clob_client")
            return

        shares = round(size_usd / pos.avg_price, 2) if pos.avg_price > 0 else 0
        if shares <= 0:
            return

        logger.info(
            "⚡ Copiando posición %s | %s shares @ %.3f | $%.0f",
            pos.title[:40], shares, pos.avg_price, size_usd,
        )

        try:
            order = self._clob.create_order({
                "token_id": pos.asset,
                "side": "BUY",
                "price": pos.avg_price,
                "size": shares,
                "order_type": "GTC",
            })
            if order:
                self._portfolio.record_order_opened(
                    STRATEGY_SMART_MONEY, pos.condition_id, size_usd
                )
                logger.info("   ✅ Orden colocada: %s", order)
            else:
                logger.warning("   ⚠️  Orden no ejecutada")
        except Exception:
            logger.exception("❌ Error ejecutando copia de %s", pos.condition_id[:12])

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        return {
            "wallets_tracked": len(self._cfg.wallets),
            "cycle": self._cycle,
            "positions_copied": len(self._copied),
            "budget_usd": self._portfolio.get_budget(STRATEGY_SMART_MONEY),
            "available_usd": self._portfolio.get_available(STRATEGY_SMART_MONEY),
        }

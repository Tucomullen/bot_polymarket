"""
backtesting/downloader.py — Descarga datos históricos desde la Gamma API de Polymarket.

Endpoints usados:
  GET /prices-history?market={tokenId}&interval=1d&fidelity=60  → precio por hora
  GET /markets?order=volume24h&limit=20&active=true             → top mercados

Los datos se cachean en backtesting/data/ para evitar re-descargas innecesarias.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger("polybot.backtest.downloader")

GAMMA_HOST = "https://gamma-api.polymarket.com"
DATA_DIR = Path(__file__).parent / "data"


# ---------------------------------------------------------------------------
# Estructuras de datos
# ---------------------------------------------------------------------------

@dataclass
class PricePoint:
    """Un punto de precio histórico (timestamp + mid-price)."""
    timestamp: float
    price: float


@dataclass
class MarketHistory:
    """Historial completo de un mercado para usar en el simulador."""
    condition_id: str
    token_id_yes: str
    question: str
    tick_size: float = 0.01
    reward_max_spread: float = 0.0
    reward_min_size: float = 0.0
    has_maker_rebates: bool = False
    has_liquidity_rewards: bool = False
    score: float = 50.0
    prices: list[PricePoint] = field(default_factory=list)

    @property
    def n_points(self) -> int:
        return len(self.prices)

    @property
    def days_covered(self) -> float:
        if len(self.prices) < 2:
            return 0.0
        return (self.prices[-1].timestamp - self.prices[0].timestamp) / 86400


# ---------------------------------------------------------------------------
# Descarga de mercados top
# ---------------------------------------------------------------------------

async def fetch_top_markets(
    n: int = 5,
    verify_ssl: bool = True,
) -> list[dict]:
    """
    Descarga los N mercados más activos de la Gamma API.
    Filtra por mercados binarios con tokens YES/NO válidos.
    """
    params = {
        "order": "volume24hrClob",
        "ascending": "false",
        "limit": str(max(n * 5, 30)),
        "active": "true",
        "closed": "false",
    }
    async with httpx.AsyncClient(timeout=20.0, verify=verify_ssl) as client:
        resp = await client.get(f"{GAMMA_HOST}/markets", params=params)
        resp.raise_for_status()
        raw = resp.json()

    markets = raw if isinstance(raw, list) else raw.get("data", [])

    valid = []
    for m in markets:
        # La API puede devolver tokens como lista de objetos o como clobTokenIds (lista de strings)
        tokens = m.get("tokens") or []
        clob_token_ids = m.get("clobTokenIds") or []
        if isinstance(clob_token_ids, str):
            import json as _json
            try:
                clob_token_ids = _json.loads(clob_token_ids)
            except Exception:
                clob_token_ids = []
        has_tokens = len(tokens) >= 2 or len(clob_token_ids) >= 2
        if not has_tokens:
            continue
        cid = m.get("conditionId") or m.get("condition_id", "")
        if not cid:
            continue
        if not m.get("active", True):
            continue
        valid.append(m)
        if len(valid) >= n:
            break

    logger.info("🔍 Top-%d mercados encontrados: %d candidatos válidos", n, len(valid))
    return valid


def _extract_token_yes(raw: dict) -> str:
    """Extrae el token ID del outcome YES de un mercado."""
    # Formato nuevo: clobTokenIds puede ser lista de strings o JSON string
    clob_ids = raw.get("clobTokenIds") or []
    if isinstance(clob_ids, str):
        import json as _json
        try:
            clob_ids = _json.loads(clob_ids)
        except Exception:
            clob_ids = []
    if clob_ids:
        return str(clob_ids[0])
    # Formato legacy: tokens es lista de objetos con outcome
    tokens = raw.get("tokens") or []
    for t in tokens:
        outcome = str(t.get("outcome", "")).upper()
        if outcome in ("YES", "TRUE", "1"):
            return t.get("token_id") or t.get("tokenId", "")
    if tokens:
        return tokens[0].get("token_id") or tokens[0].get("tokenId", "")
    return ""


# ---------------------------------------------------------------------------
# Descarga de precio histórico
# ---------------------------------------------------------------------------

async def fetch_price_history(
    token_id: str,
    days: int = 30,
    verify_ssl: bool = True,
) -> list[PricePoint]:
    """Descarga el historial de precios por hora para un token YES."""
    # interval=1m da ~30 días; interval=max da todo el historial disponible
    # No usamos startTs/endTs porque la API rechaza ventanas fuera del rango del mercado
    interval = "1m" if days <= 30 else "max"
    params = {
        "market": token_id,
        "fidelity": "60",
        "interval": interval,
    }

    clob_host = "https://clob.polymarket.com"
    async with httpx.AsyncClient(timeout=30.0, verify=verify_ssl) as client:
        resp = await client.get(f"{clob_host}/prices-history", params=params)
        resp.raise_for_status()
        data = resp.json()

    history = data.get("history", data) if isinstance(data, dict) else data

    points = []
    for item in (history or []):
        t = item.get("t") or item.get("ts") or item.get("timestamp", 0)
        p = item.get("p") or item.get("price", 0)
        try:
            price = float(p)
            ts = float(t)
        except (TypeError, ValueError):
            continue
        if 0.005 <= price <= 0.995 and ts > 0:
            points.append(PricePoint(timestamp=ts, price=price))

    points.sort(key=lambda x: x.timestamp)
    return points


# ---------------------------------------------------------------------------
# Caché local
# ---------------------------------------------------------------------------

def _cache_path(condition_id: str, days: int) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{condition_id[:16]}_{days}d.json"


def _load_cache(condition_id: str, days: int, max_age_hours: int = 6) -> list[PricePoint] | None:
    path = _cache_path(condition_id, days)
    if not path.exists():
        return None
    if (time.time() - path.stat().st_mtime) / 3600 > max_age_hours:
        return None
    try:
        raw = json.loads(path.read_text())
        return [PricePoint(timestamp=p["timestamp"], price=p["price"]) for p in raw]
    except Exception:
        return None


def _save_cache(condition_id: str, days: int, points: list[PricePoint]) -> None:
    path = _cache_path(condition_id, days)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"timestamp": p.timestamp, "price": p.price} for p in points]))


# ---------------------------------------------------------------------------
# API de alto nivel
# ---------------------------------------------------------------------------

async def download_market_history(
    market_raw: dict,
    days: int = 30,
    verify_ssl: bool = True,
) -> MarketHistory | None:
    """Descarga el historial completo de un mercado (con caché)."""
    condition_id = market_raw.get("conditionId") or market_raw.get("condition_id", "")
    token_id = _extract_token_yes(market_raw)
    question = market_raw.get("question") or market_raw.get("title", condition_id[:16])
    tick_size = float(market_raw.get("orderPriceMinTickSize", 0.01) or 0.01)

    rewards = market_raw.get("rewardsMaxSpread") or market_raw.get("rewards", {})
    if isinstance(rewards, dict):
        reward_max_spread = float(rewards.get("maxSpread", 0) or 0)
        reward_min_size = float(rewards.get("minSize", 0) or 0)
    else:
        reward_max_spread = float(market_raw.get("rewardsMaxSpread", 0) or 0)
        reward_min_size = float(market_raw.get("rewardsMinSize", 0) or 0)

    if not token_id:
        logger.warning("⚠️  Sin token_id_yes para %s", condition_id[:16])
        return None

    cached = _load_cache(condition_id, days)
    if cached:
        logger.info("📦 Caché — %s (%d puntos, %.1f días)",
                    question[:40], len(cached),
                    (cached[-1].timestamp - cached[0].timestamp) / 86400 if len(cached) > 1 else 0)
        prices = cached
    else:
        logger.info("⬇️  Descargando %dd de historia para: %s", days, question[:40])
        try:
            prices = await fetch_price_history(token_id, days, verify_ssl)
        except Exception as exc:
            logger.error("❌ Error descargando %s: %s", condition_id[:16], exc)
            return None

        if len(prices) < 10:
            logger.warning("⚠️  Datos insuficientes (%d puntos) para %s", len(prices), condition_id[:16])
            return None

        _save_cache(condition_id, days, prices)
        logger.info("   ✅ %d puntos descargados", len(prices))

    return MarketHistory(
        condition_id=condition_id,
        token_id_yes=token_id,
        question=question,
        tick_size=tick_size,
        reward_max_spread=reward_max_spread,
        reward_min_size=reward_min_size,
        has_maker_rebates=bool(market_raw.get("makerBaseFee", 0)),
        has_liquidity_rewards=bool(reward_max_spread or reward_min_size),
        score=50.0,
        prices=prices,
    )


async def download_histories(
    n_markets: int = 5,
    days: int = 30,
    verify_ssl: bool = True,
) -> list[MarketHistory]:
    """Descarga los N mercados top + su historial de precios."""
    top_markets = await fetch_top_markets(n_markets, verify_ssl)
    if not top_markets:
        logger.error("❌ No se encontraron mercados activos")
        return []

    tasks = [download_market_history(m, days, verify_ssl) for m in top_markets]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    histories = []
    for r in results:
        if isinstance(r, MarketHistory):
            histories.append(r)
        elif isinstance(r, Exception):
            logger.warning("⚠️  Error en descarga: %s", r)

    logger.info("📊 %d/%d mercados con datos válidos", len(histories), len(top_markets))
    return histories


async def download_histories_with_discovery(
    n_markets: int = 5,
    days: int = 30,
    verify_ssl: bool = True,
    min_price: float = 0.15,
    max_price: float = 0.85,
) -> list[MarketHistory]:
    """
    Descarga historiales usando MarketDiscovery para selección de mercados.

    A diferencia de download_histories() (que usa ranking bruto por volumen),
    esta función aplica el mismo pipeline de 10 factores del bot en live:
    precio cercano a 0.50, spread razonable, volumen mínimo, incentivos, etc.
    Garantiza mercados aptos para market making — no directionales.

    min_price/max_price: filtro de precio más estricto que el default del bot (0.03/0.97).
    Para backtesting se usa 0.15/0.85 para descartar outrights que no oscilan.
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from src.discovery import MarketDiscovery, DiscoveryConfig

    cfg = DiscoveryConfig(
        max_active_markets=n_markets,
        min_price=min_price,
        max_price=max_price,
    )

    # Respetar el flag VERIFY_SSL del entorno si no se especifica explícitamente
    import os
    if not verify_ssl:
        os.environ["VERIFY_SSL"] = "false"

    discovery = MarketDiscovery(cfg)
    try:
        candidates = await discovery.scan(force=True)
    finally:
        await discovery.close()

    if not candidates:
        logger.error("❌ Discovery no encontró mercados válidos")
        return []

    logger.info("🎯 Discovery seleccionó %d mercados para backtest", len(candidates))
    for i, c in enumerate(candidates, 1):
        logger.info(
            "   %d. [%.1f] %s — mid=%.2f, spread=%.1f¢, vol=$%.0f",
            i, c.score, c.question[:55], c.midpoint, c.spread_cents, c.volume_24h,
        )

    async def _download_candidate(c) -> "MarketHistory | None":
        if not c.token_id_yes:
            logger.warning("⚠️  Sin token_id_yes para %s", c.condition_id[:16])
            return None

        cached = _load_cache(c.condition_id, days)
        if cached:
            logger.info(
                "📦 Caché — %s (%d puntos, %.1f días)",
                c.question[:40], len(cached),
                (cached[-1].timestamp - cached[0].timestamp) / 86400 if len(cached) > 1 else 0,
            )
            prices = cached
        else:
            logger.info(
                "⬇️  Descargando %dd de historia: %s (score=%.1f)",
                days, c.question[:40], c.score,
            )
            try:
                prices = await fetch_price_history(c.token_id_yes, days, verify_ssl)
            except Exception as exc:
                logger.error("❌ Error descargando %s: %s", c.condition_id[:16], exc)
                return None

            if len(prices) < 10:
                logger.warning(
                    "⚠️  Datos insuficientes (%d puntos) para %s",
                    len(prices), c.condition_id[:16],
                )
                return None

            _save_cache(c.condition_id, days, prices)
            logger.info("   ✅ %d puntos descargados", len(prices))

        tick_size = 0.01
        try:
            tick_size = float(c.tick_size) if c.tick_size else 0.01
        except (TypeError, ValueError):
            pass

        return MarketHistory(
            condition_id=c.condition_id,
            token_id_yes=c.token_id_yes,
            question=c.question,
            tick_size=tick_size,
            reward_max_spread=c.reward_max_spread,
            reward_min_size=c.reward_min_size,
            has_maker_rebates=c.has_maker_rebates,
            has_liquidity_rewards=c.has_liquidity_rewards,
            score=c.score,
            prices=prices,
        )

    tasks = [_download_candidate(c) for c in candidates]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    histories = []
    for r in results:
        if isinstance(r, MarketHistory):
            histories.append(r)
        elif isinstance(r, Exception):
            logger.warning("⚠️  Error en descarga: %s", r)

    logger.info("📊 %d/%d mercados con datos válidos", len(histories), len(candidates))
    return histories

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
        tokens = m.get("tokens") or []
        if len(tokens) < 2:
            continue
        cid = m.get("conditionId") or m.get("condition_id", "")
        if not cid:
            continue
        # Solo mercados binarios activos
        if not m.get("active", True):
            continue
        valid.append(m)
        if len(valid) >= n:
            break

    logger.info("🔍 Top-%d mercados encontrados: %d candidatos válidos", n, len(valid))
    return valid


def _extract_token_yes(raw: dict) -> str:
    """Extrae el token ID del outcome YES de un mercado."""
    tokens = raw.get("tokens") or []
    for t in tokens:
        outcome = str(t.get("outcome", "")).upper()
        if outcome in ("YES", "TRUE", "1"):
            return t.get("token_id") or t.get("tokenId", "")
    # Fallback: primer token
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
    """
    Descarga el historial de precios por hora para un token YES.

    Usa el endpoint /prices-history de Gamma API con fidelidad de 60 minutos.
    """
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400

    params = {
        "market": token_id,
        "interval": "1d",
        "fidelity": "60",    # granularidad en minutos
        "startTs": str(start_ts),
        "endTs": str(end_ts),
    }

    async with httpx.AsyncClient(timeout=30.0, verify=verify_ssl) as client:
        resp = await client.get(f"{GAMMA_HOST}/prices-history", params=params)
        resp.raise_for_status()
        data = resp.json()

    # Respuesta: {"history": [{"t": ts, "p": "0.52"}, ...]}
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
    """
    Descarga el historial completo de un mercado (con caché).
    Devuelve None si no hay suficientes datos.
    """
    condition_id = market_raw.get("conditionId") or market_raw.get("condition_id", "")
    token_id = _extract_token_yes(market_raw)
    question = market_raw.get("question") or market_raw.get("title", condition_id[:16])
    tick_size = float(market_raw.get("orderPriceMinTickSize", 0.01) or 0.01)

    # Rewards info
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

    # Caché
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
        score=50.0,  # placeholder; no es crítico para el backtest
        prices=prices,
    )


async def download_histories(
    n_markets: int = 5,
    days: int = 30,
    verify_ssl: bool = True,
) -> list[MarketHistory]:
    """
    Descarga los N mercados top + su historial de precios.
    Punto de entrada principal del downloader.
    """
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

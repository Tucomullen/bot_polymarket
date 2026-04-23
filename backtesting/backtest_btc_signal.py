"""
backtesting/backtest_btc_signal.py — Validación de accuracy del señal BTC-5min.

Hipótesis: a T-10s antes de cierre, si Binance BTCUSDT > strike → predecir YES.
GATE: accuracy ≥ 90% para proceder con implementación del BTC Sniper.

Uso:
    python -m backtesting.backtest_btc_signal [--limit 200]
"""

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

GAMMA_API = "https://gamma-api.polymarket.com"
BINANCE_API = "https://api.binance.com"

# Regex para extraer el strike del título, e.g. "Will BTC exceed $67,500 at ..."
_STRIKE_RE = re.compile(r"\$[\d,]+(?:\.\d+)?")
# Regex para detectar mercados BTC de 5 minutos
_BTC_5MIN_RE = re.compile(
    r"(?:will\s+)?btc(?:usdt)?(?:\s+(?:exceed|above|below|be\s+above|be\s+below|over|under))?\s+\$[\d,]+",
    re.IGNORECASE,
)


@dataclass
class BtcMarket:
    condition_id: str
    question: str
    strike: float
    resolved_yes: bool          # True if YES token resolved to 1.0
    resolved_at_ts: int         # Unix ms


def _parse_strike(question: str) -> float | None:
    m = _STRIKE_RE.search(question)
    if not m:
        return None
    raw = m.group(0).replace("$", "").replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _is_btc_5min(question: str) -> bool:
    return bool(_BTC_5MIN_RE.search(question)) and (
        "5 min" in question.lower()
        or "5min" in question.lower()
        or "5-min" in question.lower()
        or "5 minute" in question.lower()
    )


async def _fetch_markets(client: httpx.AsyncClient, limit: int) -> list[BtcMarket]:
    """Descarga mercados BTC-5min cerrados desde Gamma API."""
    markets: list[BtcMarket] = []
    offset = 0
    page_size = 100

    while len(markets) < limit:
        resp = await client.get(
            f"{GAMMA_API}/markets",
            params={
                "closed": "true",
                "active": "false",
                "limit": page_size,
                "offset": offset,
                "order": "endDateIso",
                "ascending": "false",
            },
            timeout=20,
        )
        if resp.status_code >= 500:
            break  # Gamma API no devuelve más páginas
        resp.raise_for_status()
        items = resp.json()
        if not items:
            break

        for item in items:
            q = item.get("question", "")
            if not _is_btc_5min(q):
                continue
            strike = _parse_strike(q)
            if strike is None:
                continue

            # outcomePrices: ["1", "0"] means YES resolved; ["0", "1"] means NO resolved
            prices = item.get("outcomePrices", [])
            if not prices or len(prices) < 2:
                continue
            try:
                yes_price = float(prices[0])
            except (ValueError, TypeError):
                continue
            resolved_yes = yes_price > 0.5

            # resolvedAt puede ser ISO o ms epoch
            resolved_raw = item.get("resolvedAt") or item.get("endDateIso", "")
            if not resolved_raw:
                continue
            try:
                if isinstance(resolved_raw, (int, float)):
                    ts_ms = int(resolved_raw)
                elif "T" in str(resolved_raw):
                    dt = datetime.fromisoformat(str(resolved_raw).replace("Z", "+00:00"))
                    ts_ms = int(dt.timestamp() * 1000)
                else:
                    ts_ms = int(float(resolved_raw) * 1000)
            except Exception:
                continue

            markets.append(BtcMarket(
                condition_id=item.get("conditionId", ""),
                question=q,
                strike=strike,
                resolved_yes=resolved_yes,
                resolved_at_ts=ts_ms,
            ))

            if len(markets) >= limit:
                break

        offset += page_size
        if len(items) < page_size:
            break
        await asyncio.sleep(0.2)  # Rate limit amable

    return markets


async def _get_btc_price_at(client: httpx.AsyncClient, ts_ms: int) -> float | None:
    """
    Obtiene el precio de cierre del minuto ANTERIOR a ts_ms.
    Simula la señal que tendríamos a T-10s: usamos el cierre de la vela 1m más reciente.
    """
    # Binance klines: pedir 1 vela que contenga ts_ms - 1 minuto
    open_time = ts_ms - 60_000  # vela que cierra justo antes de ts_ms
    try:
        resp = await client.get(
            f"{BINANCE_API}/api/v3/klines",
            params={
                "symbol": "BTCUSDT",
                "interval": "1m",
                "startTime": open_time,
                "limit": 1,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        # kline format: [open_time, open, high, low, close, ...]
        return float(data[0][4])  # close price
    except Exception:
        return None


async def run_backtest(limit: int = 200) -> None:
    correct = 0
    total = 0
    no_price = 0

    async with httpx.AsyncClient() as client:
        print(f"⏳ Descargando mercados BTC-5min cerrados (límite={limit})...")
        markets = await _fetch_markets(client, limit)
        print(f"   Encontrados: {len(markets)} mercados BTC-5min con strike")

        if not markets:
            print("❌ No se encontraron mercados BTC-5min. Abortando.")
            sys.exit(2)

        print("⏳ Consultando precios Binance para cada mercado...\n")
        for i, m in enumerate(markets, 1):
            btc_price = await _get_btc_price_at(client, m.resolved_at_ts)
            if btc_price is None:
                no_price += 1
                continue

            # Señal: precio > strike → predecir YES
            signal_yes = btc_price > m.strike
            hit = signal_yes == m.resolved_yes
            if hit:
                correct += 1
            total += 1

            if i <= 10 or i % 25 == 0:  # mostrar muestra de mercados
                res_dt = datetime.fromtimestamp(m.resolved_at_ts / 1000, tz=timezone.utc)
                print(
                    f"  [{i:3d}] {res_dt:%Y-%m-%d %H:%M}Z | strike=${m.strike:,.0f} | "
                    f"btc={btc_price:,.2f} | signal={'YES' if signal_yes else 'NO '} | "
                    f"actual={'YES' if m.resolved_yes else 'NO '} | {'✓' if hit else '✗'}"
                )

            # Rate limit gentil con Binance
            if i % 10 == 0:
                await asyncio.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"  RESULTADOS DEL BACKTEST BTC-5min SNIPER")
    print(f"{'='*60}")
    print(f"  Mercados analizados : {total}")
    print(f"  Sin precio Binance  : {no_price}")
    print(f"  Correctos           : {correct}")
    accuracy = correct / total * 100 if total > 0 else 0
    print(f"  Accuracy            : {accuracy:.1f}%")
    print(f"{'='*60}")

    THRESHOLD = 90.0
    if accuracy >= THRESHOLD:
        print(f"\n  ✅ PASS — accuracy {accuracy:.1f}% ≥ {THRESHOLD}% → proceder con Fase A")
    else:
        print(f"\n  ❌ FAIL — accuracy {accuracy:.1f}% < {THRESHOLD}% → NO implementar BTC Sniper")
        print(f"     El edge no cubre el payoff asimétrico (ratio ~1:9)")
    print()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200,
                        help="Máximo de mercados BTC-5min a analizar (default: 200)")
    args = parser.parse_args()
    asyncio.run(run_backtest(args.limit))


if __name__ == "__main__":
    main()

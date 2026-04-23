"""
src/arb_scanner.py — Scanner de arbitraje YES+NO intra-plataforma.

En mercados binarios de Polymarket, YES + NO siempre resuelven a $1.00.
Si el coste de comprar ambos tokens es < $1.00, hay beneficio garantizado.

Flujo:
  1. Gamma API → mercados activos con outcomePrices como pre-filtro rápido
  2. CLOB API → best_ask real de YES y NO para los candidatos
  3. gap = 1.0 - yes_ask - no_ask  →  si gap > MIN_GAP, es oportunidad
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

logger = logging.getLogger("polybot.arb")

MIN_GAP_DEFAULT = 0.02           # 2¢ mínimo de beneficio garantizado por share
MAX_SUM_PREFILTER = 0.96         # Gamma pre-filtro: descarta mercados eficientes
MIN_HOURS = 1.0                  # No entrar si resuelve en < 1h (riesgo de resolución)
MAX_HOURS = 24 * 30              # No bloquear capital > 30 días


@dataclass
class ArbOpportunity:
    condition_id: str
    question: str
    token_id_yes: str
    token_id_no: str
    yes_ask: float          # Precio a pagar por 1 share de YES (taker)
    no_ask: float           # Precio a pagar por 1 share de NO (taker)
    gap: float              # 1.0 - yes_ask - no_ask → beneficio por share
    hours_to_resolution: float
    end_date: str = ""

    @property
    def gap_pct(self) -> float:
        total = self.yes_ask + self.no_ask
        return self.gap / total * 100 if total > 0 else 0.0

    @property
    def cost_per_share(self) -> float:
        return self.yes_ask + self.no_ask


@dataclass
class ArbScannerConfig:
    min_gap: float = MIN_GAP_DEFAULT
    max_sum_prefilter: float = MAX_SUM_PREFILTER
    min_hours: float = MIN_HOURS
    max_hours: float = MAX_HOURS
    max_clob_checks: int = 60      # Máx llamadas CLOB por scan (rate limit)


class ArbScanner:
    """
    Escanea mercados activos de Polymarket buscando arb YES+NO.

    Uso:
        scanner = ArbScanner()
        opps = await scanner.scan()
        for opp in opps:
            print(opp.question, opp.gap_pct)
        await scanner.close()
    """

    def __init__(self, cfg: ArbScannerConfig | None = None):
        self.cfg = cfg or ArbScannerConfig()
        self._client: httpx.AsyncClient | None = None

    async def _client_(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def scan(self) -> list[ArbOpportunity]:
        """Devuelve oportunidades de arb ordenadas por gap descendente."""
        client = await self._client_()
        candidates = await self._fetch_gamma_candidates(client)
        logger.info("🔍 Arb scan: %d candidatos pre-filtro (Gamma)", len(candidates))

        opportunities: list[ArbOpportunity] = []
        for cand in candidates[: self.cfg.max_clob_checks]:
            opp = await self._verify_clob(client, cand)
            if opp:
                opportunities.append(opp)
            await asyncio.sleep(0.1)

        opportunities.sort(key=lambda o: o.gap, reverse=True)
        return opportunities

    # ------------------------------------------------------------------
    # Paso 1: Gamma API — pre-filtro barato
    # ------------------------------------------------------------------

    async def _fetch_gamma_candidates(self, client: httpx.AsyncClient) -> list[dict]:
        candidates: list[dict] = []
        now = datetime.now(timezone.utc)
        offset = 0

        while len(candidates) < self.cfg.max_clob_checks * 3:
            try:
                r = await client.get(
                    f"{GAMMA_API}/markets",
                    params={"active": "true", "closed": "false",
                            "limit": 100, "offset": offset},
                )
                if r.status_code >= 400:
                    break
                items = r.json()
                if not items:
                    break

                for m in items:
                    cand = self._parse_candidate(m, now)
                    if cand:
                        candidates.append(cand)

                offset += 100
                if len(items) < 100:
                    break
                await asyncio.sleep(0.2)

            except Exception:
                logger.exception("Error fetching Gamma markets")
                break

        return candidates

    def _parse_candidate(self, m: dict, now: datetime) -> dict | None:
        prices = m.get("outcomePrices", [])
        if len(prices) < 2:
            return None
        try:
            yes_p = float(prices[0])
            no_p = float(prices[1])
        except (ValueError, TypeError):
            return None

        if yes_p + no_p > self.cfg.max_sum_prefilter:
            return None
        if yes_p <= 0 or no_p <= 0:
            return None

        end_raw = m.get("endDateIso", "")
        if not end_raw:
            return None
        try:
            end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            hours = (end_dt - now).total_seconds() / 3600
        except Exception:
            return None
        if not (self.cfg.min_hours <= hours <= self.cfg.max_hours):
            return None

        clob_ids = m.get("clobTokenIds", [])
        if isinstance(clob_ids, str):
            try:
                clob_ids = json.loads(clob_ids)
            except Exception:
                return None
        if len(clob_ids) < 2:
            return None

        return {
            "condition_id": m.get("conditionId", ""),
            "question": m.get("question", ""),
            "token_id_yes": clob_ids[0],
            "token_id_no": clob_ids[1],
            "hours": hours,
            "end_date": end_raw,
        }

    # ------------------------------------------------------------------
    # Paso 2: CLOB — verificar precios reales
    # ------------------------------------------------------------------

    async def _verify_clob(self, client: httpx.AsyncClient, cand: dict) -> ArbOpportunity | None:
        try:
            yes_ask = await self._best_ask(client, cand["token_id_yes"])
            no_ask = await self._best_ask(client, cand["token_id_no"])
        except Exception:
            return None

        if yes_ask is None or no_ask is None:
            return None
        if yes_ask <= 0 or no_ask <= 0:
            return None

        gap = 1.0 - yes_ask - no_ask
        if gap < self.cfg.min_gap:
            return None

        return ArbOpportunity(
            condition_id=cand["condition_id"],
            question=cand["question"],
            token_id_yes=cand["token_id_yes"],
            token_id_no=cand["token_id_no"],
            yes_ask=yes_ask,
            no_ask=no_ask,
            gap=gap,
            hours_to_resolution=cand["hours"],
            end_date=cand["end_date"],
        )

    async def _best_ask(self, client: httpx.AsyncClient, token_id: str) -> float | None:
        r = await client.get(f"{CLOB_API}/book", params={"token_id": token_id})
        if r.status_code != 200:
            return None
        asks = r.json().get("asks", [])
        if not asks:
            return None
        return min(float(a["price"]) for a in asks)

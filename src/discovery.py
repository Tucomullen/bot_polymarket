"""
src/discovery.py — Descubrimiento y Selección Automática de Mercados.

Este módulo NO forma parte de la lógica de trading. Es un módulo previo
que responde a la pregunta: "¿En qué mercados debería estar haciendo
market making ahora mismo?"

Flujo:
  1. Consulta la Gamma API para obtener mercados activos
  2. Consulta el endpoint de rewards para saber cuáles tienen incentivos
  3. Consulta el CLOB para fee-rate y orderbook superficial de cada candidato
  4. Aplica filtros duros (descarta lo que no cumple mínimos)
  5. Calcula un score configurable para cada mercado superviviente
  6. Devuelve un ranking ordenado de los N mejores

Supuestos sobre la API de Polymarket (encapsulados para corrección rápida):
  ⚠️ SUPUESTO 1: La Gamma API devuelve un campo 'tags' con la categoría
     (ej. "crypto", "sports", "politics"). Si cambia, ajustar _classify_category().
  ⚠️ SUPUESTO 2: El endpoint /rewards del CLOB devuelve mercados elegibles
     para liquidity rewards con campos como daily_reward, max_spread, min_size.
     Si Polymarket cambia este endpoint, ajustar _fetch_reward_markets().
  ⚠️ SUPUESTO 3: El fee-rate endpoint GET /fee-rate?tokenID={id} devuelve
     un JSON con "fee_rate_bps". Si retorna 0 para un mercado, asumimos
     que ese mercado no tiene taker fees (y por tanto tampoco maker rebates).
  ⚠️ SUPUESTO 4: Los mercados crypto de corto plazo se identifican por
     patrones en el slug/question como "btc", "eth", "5-min", "15-min".
     Si Polymarket cambia la nomenclatura, ajustar _CRYPTO_SHORT_TERM_PATTERNS.
"""

import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import httpx

logger = logging.getLogger("polybot.discovery")

_VERIFY_SSL = os.getenv("VERIFY_SSL", "true").lower() != "false"


# ===========================================================================
# Configuración del selector (valores por defecto, sobreescribibles)
# ===========================================================================

@dataclass
class DiscoveryConfig:
    """Parámetros configurables del Market Selector."""

    # Conexión
    gamma_host: str = "https://gamma-api.polymarket.com"
    clob_host: str = "https://clob.polymarket.com"

    # Cuántos mercados operar simultáneamente
    max_active_markets: int = 5

    # Score mínimo para considerar un mercado
    min_score_threshold: float = 35.0

    # Intervalo de re-escaneo (segundos)
    refresh_interval_sec: int = 300

    # --- Filtros duros (si no pasa, se descarta) ---
    min_volume_24h: float = 100.0        # USD (relajado para capturar más mercados)
    max_spread_cents: float = 15.0       # Céntimos (relajado; el scoring penalizará spreads altos)
    min_price: float = 0.03              # Evitar mercados decididos
    max_price: float = 0.97
    min_time_to_resolution_hours: float = 0.25

    # --- Pesos del scoring (suman ~1.0) ---
    w_category: float = 0.15
    w_maker_rebates: float = 0.15
    w_liquidity_rewards: float = 0.12
    w_volume: float = 0.10
    w_spread: float = 0.12
    w_price_centrality: float = 0.06
    w_trade_frequency: float = 0.06
    w_time_to_resolution: float = 0.06
    w_event_risk: float = 0.06
    w_competition: float = 0.12

    # --- Listas de control ---
    whitelist_conditions: list[str] = field(default_factory=list)
    blacklist_conditions: list[str] = field(default_factory=list)
    blacklist_tags: list[str] = field(default_factory=lambda: ["adult"])


# ===========================================================================
# Categorías con prioridad
# ===========================================================================

class MarketCategory(IntEnum):
    """Prioridad de categoría (menor = mejor)."""
    CRYPTO_SHORT_TERM = 1
    SPORTS_ESPORTS = 2
    FINANCE_ECONOMICS = 3
    TECH_WEATHER = 3
    POLITICS_GEOPOLITICS = 4
    UNKNOWN = 5


# Patrones para clasificar mercados (SUPUESTO 4)
_CRYPTO_SHORT_TERM_PATTERNS = re.compile(
    r"(btc|bitcoin|eth|ethereum|sol|solana).*(5.?min|15.?min|up.?or.?down|above|below)",
    re.IGNORECASE,
)
_CRYPTO_GENERAL_PATTERNS = re.compile(
    r"(btc|bitcoin|eth|ethereum|sol|solana|crypto|coin)", re.IGNORECASE
)
_SPORTS_PATTERNS = re.compile(
    r"(nba|nfl|mlb|nhl|soccer|football|tennis|f1|ufc|mma|ncaa|serie.a|"
    r"premier.league|esport|league.of.legends|dota|cs2|valorant)",
    re.IGNORECASE,
)
_FINANCE_PATTERNS = re.compile(
    r"(fed|interest.rate|gdp|inflation|nasdaq|s&p|stock|earnings|treasury|"
    r"unemployment|cpi|fomc|tariff)",
    re.IGNORECASE,
)
_TECH_WEATHER_PATTERNS = re.compile(
    r"(ai|openai|google|apple|tesla|spacex|hurricane|earthquake|temperature|"
    r"weather|storm|wildfire)",
    re.IGNORECASE,
)
_POLITICS_PATTERNS = re.compile(
    r"(president|election|congress|senate|governor|prime.minister|parliament|"
    r"trump|biden|war|nato|china|russia|iran|ukraine|geopolit)",
    re.IGNORECASE,
)


# ===========================================================================
# Datos de un mercado candidato
# ===========================================================================

@dataclass
class MarketCandidate:
    """Toda la info recopilada de un mercado para el scoring."""

    # Identificadores
    condition_id: str = ""
    question: str = ""
    slug: str = ""
    token_id_yes: str = ""
    token_id_no: str = ""
    neg_risk: bool = False
    tick_size: str = "0.01"

    # Categorización
    tags: list[str] = field(default_factory=list)
    category: MarketCategory = MarketCategory.UNKNOWN

    # Datos de mercado
    volume_24h: float = 0.0
    liquidity: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread_cents: float = 99.0
    midpoint: float = 0.5
    last_trade_price: float = 0.0
    recent_trade_count: int = 0        # Trades en las últimas horas

    # Tiempo
    end_date: str = ""
    hours_to_resolution: float = 9999.0

    # Incentivos
    has_taker_fees: bool = False
    fee_rate_bps: int = 0              # 0 = sin taker fees
    has_maker_rebates: bool = False
    has_liquidity_rewards: bool = False
    daily_reward_usd: float = 0.0
    reward_max_spread: float = 0.0
    reward_min_size: float = 0.0

    # Competencia
    reward_competitors: int = 0        # Cuántos makers compiten por rewards

    # Scoring
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)


# ===========================================================================
# Motor de Descubrimiento
# ===========================================================================

class MarketDiscovery:
    """
    Descubre, filtra y puntúa mercados de Polymarket para market making.

    Uso:
        discovery = MarketDiscovery(config)
        ranked = await discovery.scan()
        for m in ranked[:5]:
            print(f"{m.question} — score={m.score:.1f}")
    """

    def __init__(self, cfg: DiscoveryConfig | None = None):
        self.cfg = cfg or DiscoveryConfig()
        self._http = httpx.AsyncClient(timeout=15.0, verify=_VERIFY_SSL)
        self._reward_markets: dict[str, dict] = {}  # cache de rewards
        self._last_scan: float = 0.0
        self._cached_ranking: list[MarketCandidate] = []

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    async def scan(self, force: bool = False) -> list[MarketCandidate]:
        """
        Ejecuta el pipeline completo de discovery.
        Retorna lista ordenada por score descendente.

        Si se llama antes de que expire refresh_interval_sec y force=False,
        devuelve el cache.
        """
        now = time.time()
        if (
            not force
            and self._cached_ranking
            and (now - self._last_scan) < self.cfg.refresh_interval_sec
        ):
            return self._cached_ranking

        logger.info("🔍 Iniciando escaneo de mercados...")

        # 1. Obtener mercados activos de Gamma
        #    (Los datos de rewards vienen incluidos en cada mercado)
        raw_markets = await self._fetch_gamma_markets()
        logger.info("   📊 %d mercados activos obtenidos de Gamma", len(raw_markets))

        # 3. Construir candidatos
        candidates = []
        for raw in raw_markets:
            c = self._parse_candidate(raw)
            if c:
                candidates.append(c)

        logger.info("   🏗️  %d candidatos parseados", len(candidates))

        # 4. Filtros duros (antes del enriquecimiento para reducir llamadas al CLOB)
        filtered = self._apply_hard_filters(candidates)
        logger.info("   🔬 %d candidatos tras filtros duros", len(filtered))

        # 5. Enriquecer solo los candidatos que pasaron los filtros
        await self._enrich_candidates(filtered)

        # 6. Scoring
        for c in filtered:
            self._compute_score(c)

        # 7. Ordenar por score
        filtered.sort(key=lambda x: x.score, reverse=True)

        # 8. Tomar top N
        top = filtered[: self.cfg.max_active_markets]

        self._cached_ranking = top
        self._last_scan = now

        if top:
            logger.info("🏆 Top %d mercados seleccionados:", len(top))
            for i, m in enumerate(top, 1):
                logger.info(
                    "   %d. [%.1f] %s — spread=%.1f¢, vol=$%.0f, rebates=%s, rewards=%s",
                    i,
                    m.score,
                    m.question[:60],
                    m.spread_cents,
                    m.volume_24h,
                    "✅" if m.has_maker_rebates else "❌",
                    "✅" if m.has_liquidity_rewards else "❌",
                )
        else:
            logger.warning("⚠️  No se encontraron mercados que pasen los filtros")

        return top

    # ------------------------------------------------------------------
    # Paso 1: Gamma API
    # ------------------------------------------------------------------

    async def _fetch_gamma_markets(self) -> list[dict]:
        """Obtiene mercados activos de la Gamma API."""
        all_markets: list[dict] = []
        cursor = ""
        limit = 100

        try:
            while True:
                params: dict[str, Any] = {
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                }
                if cursor:
                    params["next_cursor"] = cursor

                resp = await self._http.get(
                    f"{self.cfg.gamma_host}/markets",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()

                # La Gamma API devuelve una lista directa o un objeto con paginación
                if isinstance(data, list):
                    all_markets.extend(data)
                    break  # Sin paginación explícita
                elif isinstance(data, dict):
                    markets = data.get("data", data.get("markets", []))
                    all_markets.extend(markets)
                    cursor = data.get("next_cursor", "")
                    if not cursor or cursor == "LTE=" or not markets:
                        break
                else:
                    break

        except httpx.HTTPError as exc:
            logger.error("❌ Error consultando Gamma API: %s", exc)

        return all_markets

    # ------------------------------------------------------------------
    # Paso 2: Parseo de candidatos
    # ------------------------------------------------------------------

    def _parse_candidate(self, raw: dict) -> MarketCandidate | None:
        """Convierte un market de la Gamma API en MarketCandidate."""
        try:
            # La Gamma API usa camelCase
            condition_id = raw.get("conditionId", raw.get("condition_id", ""))

            # Listas de control
            if self.cfg.whitelist_conditions:
                if condition_id not in self.cfg.whitelist_conditions:
                    return None

            if condition_id in self.cfg.blacklist_conditions:
                return None

            # Extraer tags (la API devuelve tags dentro de events[])
            tags = []
            for event in raw.get("events", []):
                for t in event.get("tags", []):
                    tag_str = t.get("label", t) if isinstance(t, dict) else str(t)
                    tags.append(tag_str.lower())

            # Blacklist de tags
            for btag in self.cfg.blacklist_tags:
                if btag.lower() in tags:
                    return None

            # Tokens — la Gamma API devuelve clobTokenIds como string JSON
            import json as _json
            clob_token_ids = raw.get("clobTokenIds", [])
            if isinstance(clob_token_ids, str):
                try:
                    clob_token_ids = _json.loads(clob_token_ids)
                except Exception:
                    clob_token_ids = []
            token_yes = ""
            token_no = ""
            if isinstance(clob_token_ids, list) and len(clob_token_ids) >= 2:
                token_yes = str(clob_token_ids[0])
                token_no = str(clob_token_ids[1])
            elif isinstance(clob_token_ids, list) and len(clob_token_ids) == 1:
                token_yes = str(clob_token_ids[0])

            # Precios — la Gamma API los devuelve directamente como bestBid/bestAsk
            best_bid = float(raw.get("bestBid", 0) or 0)
            best_ask = float(raw.get("bestAsk", 0) or 0)
            last_trade = float(raw.get("lastTradePrice", 0) or 0)

            # Midpoint: preferir bestBid/bestAsk, fallback a outcomePrices
            if best_bid > 0 and best_ask > 0:
                midpoint = (best_bid + best_ask) / 2.0
                spread_cents = round((best_ask - best_bid) * 100, 2)
            else:
                # Fallback a outcomePrices
                outcome_prices = raw.get("outcomePrices", "")
                midpoint = 0.5
                spread_cents = 99.0
                if isinstance(outcome_prices, str) and outcome_prices:
                    try:
                        import json as _json
                        prices = _json.loads(outcome_prices)
                        midpoint = float(prices[0]) if prices else 0.5
                    except Exception:
                        pass
                elif isinstance(outcome_prices, list) and outcome_prices:
                    midpoint = float(outcome_prices[0])

            question = raw.get("question", raw.get("title", ""))
            slug = raw.get("slug", "")

            # Clasificar categoría
            category = self._classify_category(question, slug, tags)

            # Volumen — preferir volumeClob (más preciso) o volume24hr
            volume_24h = float(raw.get("volume24hrClob", raw.get("volume24hr", 0)) or 0)

            # Liquidez
            liquidity = float(raw.get("liquidityClob", raw.get("liquidity", 0)) or 0)

            # Fecha de finalización
            end_date = raw.get("endDateIso", raw.get("endDate", ""))
            hours_to_res = self._hours_until(end_date)

            # Rewards — datos en clobRewards[] y campos raíz
            clob_rewards = raw.get("clobRewards", [])
            rewards_daily_rate = 0.0
            if clob_rewards and isinstance(clob_rewards, list):
                # Sumar las tasas diarias de todos los reward programs activos
                rewards_daily_rate = sum(
                    float(r.get("rewardsDailyRate", 0) or 0) for r in clob_rewards
                )

            rewards_min_size = float(raw.get("rewardsMinSize", 0) or 0)
            rewards_max_spread = float(raw.get("rewardsMaxSpread", 0) or 0)
            reward_competitors = int(raw.get("competitive", 0) or 0)

            has_liq_rewards = rewards_daily_rate > 0

            c = MarketCandidate(
                condition_id=condition_id,
                question=question,
                slug=slug,
                token_id_yes=token_yes,
                token_id_no=token_no,
                neg_risk=raw.get("negRisk", False) or False,
                tick_size=str(raw.get("orderPriceMinTickSize", "0.01") or "0.01"),
                tags=tags,
                category=category,
                volume_24h=volume_24h,
                liquidity=liquidity,
                best_bid=best_bid,
                best_ask=best_ask,
                spread_cents=spread_cents,
                midpoint=midpoint,
                last_trade_price=last_trade,
                end_date=end_date,
                hours_to_resolution=hours_to_res,
                has_liquidity_rewards=has_liq_rewards,
                daily_reward_usd=rewards_daily_rate,
                reward_max_spread=rewards_max_spread,
                reward_min_size=rewards_min_size,
                reward_competitors=reward_competitors,
            )
            return c

        except Exception as exc:
            logger.debug("⚠️  Error parseando mercado: %s", exc)
            return None

    def _classify_category(
        self, question: str, slug: str, tags: list[str]
    ) -> MarketCategory:
        """
        Clasifica un mercado en categoría según su texto y tags.
        ⚠️ SUPUESTO 1 y 4: Depende de la nomenclatura de Polymarket.
        """
        text = f"{question} {slug} {' '.join(tags)}"

        if _CRYPTO_SHORT_TERM_PATTERNS.search(text):
            return MarketCategory.CRYPTO_SHORT_TERM
        if _SPORTS_PATTERNS.search(text):
            return MarketCategory.SPORTS_ESPORTS
        if _FINANCE_PATTERNS.search(text):
            return MarketCategory.FINANCE_ECONOMICS
        if _TECH_WEATHER_PATTERNS.search(text):
            return MarketCategory.TECH_WEATHER
        if _POLITICS_PATTERNS.search(text):
            return MarketCategory.POLITICS_GEOPOLITICS
        if _CRYPTO_GENERAL_PATTERNS.search(text):
            return MarketCategory.CRYPTO_SHORT_TERM  # Crypto genérico → tier 1
        return MarketCategory.UNKNOWN

    @staticmethod
    def _hours_until(iso_date: str) -> float:
        """Calcula horas hasta una fecha ISO. Retorna 9999 si no parseable."""
        if not iso_date:
            return 9999.0
        try:
            from datetime import datetime, timezone

            # Manejar varios formatos
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(iso_date, fmt).replace(tzinfo=timezone.utc)
                    delta = dt - datetime.now(timezone.utc)
                    return max(delta.total_seconds() / 3600.0, 0.0)
                except ValueError:
                    continue
        except Exception:
            pass
        return 9999.0

    # ------------------------------------------------------------------
    # Paso 4: Enriquecer con datos del CLOB
    # ------------------------------------------------------------------

    async def _enrich_candidates(self, candidates: list[MarketCandidate]) -> None:
        """
        Enriquece candidatos con datos del CLOB: fee-rate y top-of-book.
        Usa un semáforo para no saturar la API (máx 5 llamadas paralelas).
        """
        import asyncio as _asyncio

        sem = _asyncio.Semaphore(5)

        async def _enrich_one(c: MarketCandidate) -> None:
            if not c.token_id_yes:
                return
            async with sem:
                await _asyncio.gather(
                    self._fetch_fee_rate(c),
                    self._fetch_top_of_book(c),
                    return_exceptions=True,
                )

        await _asyncio.gather(*[_enrich_one(c) for c in candidates], return_exceptions=True)

        rebates = sum(1 for c in candidates if c.has_maker_rebates)
        logger.info(
            "   💸 %d candidatos enriquecidos — %d con maker rebates",
            len(candidates),
            rebates,
        )

    async def _fetch_fee_rate(self, c: MarketCandidate) -> None:
        """
        Consulta GET /fee-rate?tokenID={id}.
        Si retorna > 0, el mercado tiene taker fees → maker rebates.
        ⚠️ SUPUESTO 3.
        """
        try:
            resp = await self._http.get(
                f"{self.cfg.clob_host}/fee-rate",
                params={"tokenID": c.token_id_yes},
            )
            if resp.status_code == 200:
                data = resp.json()
                bps = int(data.get("fee_rate_bps", data.get("feeRateBps", 0)) or 0)
                c.fee_rate_bps = bps
                c.has_taker_fees = bps > 0
                c.has_maker_rebates = bps > 0  # Rebates existen donde hay taker fees
        except Exception as exc:
            logger.debug("   ⚠️  fee-rate error para %s: %s", c.condition_id[:12], exc)

    async def _fetch_top_of_book(self, c: MarketCandidate) -> None:
        """Consulta el orderbook para obtener best bid/ask y spread."""
        try:
            resp = await self._http.get(
                f"{self.cfg.clob_host}/book",
                params={"token_id": c.token_id_yes},
            )
            if resp.status_code == 200:
                data = resp.json()
                bids = data.get("bids", [])
                asks = data.get("asks", [])

                if bids:
                    c.best_bid = float(bids[0].get("price", 0))
                if asks:
                    c.best_ask = float(asks[0].get("price", 1))

                if c.best_bid > 0 and c.best_ask > 0:
                    c.spread_cents = round((c.best_ask - c.best_bid) * 100, 2)
                    c.midpoint = (c.best_bid + c.best_ask) / 2.0
        except Exception as exc:
            logger.debug("   ⚠️  book error para %s: %s", c.condition_id[:12], exc)

    # ------------------------------------------------------------------
    # Paso 5: Filtros duros
    # ------------------------------------------------------------------

    def _apply_hard_filters(self, candidates: list[MarketCandidate]) -> list[MarketCandidate]:
        """Descarta mercados que no cumplen los mínimos."""
        result = []
        for c in candidates:
            # Volumen (si la Gamma API reportó 0, puede ser que el campo sea distinto)
            if c.volume_24h < self.cfg.min_volume_24h and c.volume_24h > 0:
                continue
            # Spread — solo filtrar si realmente obtuvimos datos del book
            # (spread_cents=99 significa que no pudimos leer el book)
            if c.spread_cents < 90 and c.spread_cents > self.cfg.max_spread_cents:
                continue
            # Precio (evitar mercados ya decididos)
            mid = c.midpoint
            if mid < self.cfg.min_price or mid > self.cfg.max_price:
                continue
            # Tiempo hasta resolución
            if c.hours_to_resolution < self.cfg.min_time_to_resolution_hours:
                continue

            result.append(c)

        return result

    # ------------------------------------------------------------------
    # Paso 6: Scoring
    # ------------------------------------------------------------------

    def _compute_score(self, c: MarketCandidate) -> None:
        """
        Calcula el score compuesto del mercado.
        Cada componente se normaliza a [0, 100] y se pondera.
        """
        scores: dict[str, float] = {}

        # 1. Categoría (mejor categoría = mayor score)
        #    cat 1 → 100, cat 2 → 75, cat 3 → 50, cat 4 → 30, cat 5 → 10
        cat_scores = {1: 100, 2: 75, 3: 50, 4: 30, 5: 10}
        scores["category"] = cat_scores.get(int(c.category), 10)

        # 2. Maker rebates (binario)
        scores["maker_rebates"] = 100.0 if c.has_maker_rebates else 0.0

        # 3. Liquidity rewards (binario + monto)
        if c.has_liquidity_rewards:
            # Normalizar: $100/día = 100 puntos, $10/día = 50, $0 = 0
            scores["liquidity_rewards"] = min(100.0, c.daily_reward_usd * 1.0)
        else:
            scores["liquidity_rewards"] = 0.0

        # 4. Volumen 24h (logarítmico)
        #    $1k = 30, $10k = 60, $100k = 80, $1M+ = 100
        import math
        if c.volume_24h > 0:
            scores["volume"] = min(100.0, 20.0 * math.log10(max(c.volume_24h, 1)))
        else:
            scores["volume"] = 0.0

        # 5. Spread (menor = mejor)
        #    0.5¢ = 100, 2¢ = 70, 5¢ = 30, 8¢+ = 0
        if c.spread_cents <= 0.5:
            scores["spread"] = 100.0
        elif c.spread_cents >= 8.0:
            scores["spread"] = 0.0
        else:
            scores["spread"] = max(0.0, 100.0 - (c.spread_cents - 0.5) * 13.3)

        # 6. Centralidad del precio (cercanía a 0.50)
        #    0.50 = 100, 0.30/0.70 = 50, 0.10/0.90 = 10
        distance = abs(c.midpoint - 0.50)
        scores["price_centrality"] = max(0.0, 100.0 - distance * 200.0)

        # 7. Frecuencia de trades (más = mejor, normalizado)
        scores["trade_frequency"] = min(100.0, c.recent_trade_count * 2.0)

        # 8. Tiempo hasta resolución
        #    Muy corto (<1h) = penalizado, medio (1-48h) = ideal, largo (>7d) = ok pero menos
        if c.hours_to_resolution < 1:
            scores["time_to_resolution"] = 20.0
        elif c.hours_to_resolution <= 48:
            scores["time_to_resolution"] = 100.0
        elif c.hours_to_resolution <= 168:  # 7 días
            scores["time_to_resolution"] = 70.0
        else:
            scores["time_to_resolution"] = 40.0

        # 9. Riesgo de evento brusco (penalización)
        #    Crypto corto plazo = bajo riesgo (predecible), política = alto
        event_risk_map = {
            MarketCategory.CRYPTO_SHORT_TERM: 90.0,  # Bajo riesgo de evento brusco
            MarketCategory.SPORTS_ESPORTS: 70.0,
            MarketCategory.FINANCE_ECONOMICS: 60.0,
            MarketCategory.TECH_WEATHER: 50.0,
            MarketCategory.POLITICS_GEOPOLITICS: 30.0,
            MarketCategory.UNKNOWN: 40.0,
        }
        scores["event_risk"] = event_risk_map.get(c.category, 40.0)

        # 10. Competencia de otros makers (menos = mejor)
        if c.reward_competitors <= 2:
            scores["competition"] = 100.0
        elif c.reward_competitors <= 10:
            scores["competition"] = 70.0
        elif c.reward_competitors <= 50:
            scores["competition"] = 40.0
        else:
            scores["competition"] = 10.0

        # --- Score final ponderado ---
        weights = {
            "category": self.cfg.w_category,
            "maker_rebates": self.cfg.w_maker_rebates,
            "liquidity_rewards": self.cfg.w_liquidity_rewards,
            "volume": self.cfg.w_volume,
            "spread": self.cfg.w_spread,
            "price_centrality": self.cfg.w_price_centrality,
            "trade_frequency": self.cfg.w_trade_frequency,
            "time_to_resolution": self.cfg.w_time_to_resolution,
            "event_risk": self.cfg.w_event_risk,
            "competition": self.cfg.w_competition,
        }

        total = sum(scores[k] * weights[k] for k in scores)
        c.score = round(total, 2)
        c.score_breakdown = {k: round(scores[k] * weights[k], 2) for k in scores}

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------

    def explain_score(self, c: MarketCandidate) -> str:
        """Devuelve un string legible explicando el desglose del score."""
        lines = [
            f"📊 {c.question[:70]}",
            f"   Score total: {c.score:.1f}/100",
            f"   Categoría: {c.category.name} | Spread: {c.spread_cents:.1f}¢ | Vol24h: ${c.volume_24h:,.0f}",
            f"   Rebates: {'✅' if c.has_maker_rebates else '❌'} | Rewards: {'✅' if c.has_liquidity_rewards else '❌'} (${c.daily_reward_usd:.1f}/día)",
            f"   Fee rate: {c.fee_rate_bps} bps | Competidores: {c.reward_competitors}",
            "   Desglose:",
        ]
        for k, v in sorted(c.score_breakdown.items(), key=lambda x: -x[1]):
            bar = "█" * int(v / 2) + "░" * (50 - int(v / 2))
            lines.append(f"     {k:22s} {v:5.1f}  {bar}")
        return "\n".join(lines)

"""
main.py — Punto de entrada del bot de trading Polymarket.

Fase 1 (Prompt 1): Conexión, autenticación, WebSocket y Discovery.
Ejecuta el flujo:
  1. Carga configuración desde .env
  2. Autenticación L1 + L2 contra el CLOB
  3. Discovery: escanea y rankea los mejores mercados automáticamente
  4. Conexión WebSocket a canales market + user
  5. Heartbeat automático cada 10 s
  6. Logging de eventos en consola (modo simulación)
"""

import asyncio
import logging
import signal
import sys
import os
from typing import Any

# ---------------------------------------------------------------------------
# SSL bypass (proxy corporativo con certificado auto-firmado)
# Activar con VERIFY_SSL=false en .env
# ---------------------------------------------------------------------------
# Cargamos .env manualmente aquí porque load_dotenv() ocurre más tarde (en settings.py)
from pathlib import Path as _Path
from dotenv import load_dotenv as _load_dotenv
_load_dotenv(_Path(__file__).resolve().parent / ".env")

if os.getenv("VERIFY_SSL", "true").lower() == "false":
    import httpx
    # py_clob_client crea _http_client = httpx.Client(http2=True) al importar el módulo.
    # Hay que reemplazarlo por uno con verify=False ANTES de cualquier llamada a la API.
    import py_clob_client.http_helpers.helpers as _clob_helpers
    _clob_helpers._http_client = httpx.Client(http2=True, verify=False)  # type: ignore[assignment]

from config.settings import load_config
from src.auth import Authenticator
from src.websocket_manager import WebSocketManager, WSSAuth
from src.orderbook import OrderbookTracker
from src.discovery import MarketDiscovery, DiscoveryConfig, MarketCandidate
from src.trading_loop import TradingLoop
from src.quoting import QuotePair
from src.risk_manager import RiskManager
from src.dashboard import DashboardServer, BotState, DashboardLogHandler
from src.telegram_alerts import TelegramAlerter


def _optional_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(name)-18s │ %(levelname)-5s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("polybot.main")

# Estado compartido del dashboard (módulo-level para acceso desde callbacks)
bot_state = BotState()


# ---------------------------------------------------------------------------
# Callbacks de eventos WebSocket
# ---------------------------------------------------------------------------

orderbook_tracker = OrderbookTracker()
trading_loop: TradingLoop | None = None  # Se inicializa tras el discovery
telegram: TelegramAlerter = TelegramAlerter.from_env()
_market_questions: dict[str, str] = {}  # condition_id → question (para notificaciones de fills)
_simulation_mode: bool = True


async def on_market_event(data: dict[str, Any] | list) -> None:
    """
    Callback para eventos del canal market.
    Despacha según event_type al tracker del orderbook.
    El canal market puede enviar una lista de eventos o un único dict.
    """
    if isinstance(data, list):
        for item in data:
            await on_market_event(item)
        return
    event_type = data.get("event_type", "")

    if event_type == "book":
        orderbook_tracker.process_book_event(data)
    elif event_type == "price_change":
        orderbook_tracker.process_price_change(data)
    elif event_type == "last_trade_price":
        orderbook_tracker.process_last_trade(data)
    else:
        logger.debug("📨 Evento market no clasificado: %s", event_type)


async def on_user_event(data: dict[str, Any]) -> None:
    """
    Callback para eventos del canal user.
    Despacha a:
      - Trading loop (para procesar fills y actualizar inventario)
      - Log (para visibilidad)
    """
    event_type = data.get("event_type", "")

    # Despachar al trading loop si está activo
    if trading_loop is not None:
        trading_loop.on_user_event(data)

    if event_type == "trade":
        logger.info(
            "💰 TRADE recibido — side=%s, price=%s, size=%s, status=%s",
            data.get("side"),
            data.get("price"),
            data.get("size"),
            data.get("status"),
        )
        # Notificar fill por Telegram
        _cid = data.get("market", "")
        asyncio.create_task(telegram.send_fill(
            side=data.get("side", ""),
            price=float(data.get("price", 0) or 0),
            size=float(data.get("size", 0) or 0),
            question=_market_questions.get(_cid, _cid[:20]),
            simulation=_simulation_mode,
        ))
        bot_state.push_event({
            "type": "TRADE",
            "side": data.get("side", ""),
            "price": data.get("price", ""),
            "question": data.get("market", "")[:30],
            "condition_id": data.get("market", ""),
            "simulation": False,
        })
    elif event_type == "order":
        logger.info(
            "📋 ORDER update — type=%s, side=%s, price=%s, outcome=%s",
            data.get("type"),
            data.get("side"),
            data.get("price"),
            data.get("outcome"),
        )
        bot_state.push_event({
            "type": "ORDER",
            "side": data.get("side", ""),
            "price": data.get("price", ""),
            "question": "",
            "condition_id": "",
            "simulation": False,
        })
    else:
        logger.debug("📨 Evento user no clasificado: %s", event_type)


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

async def run_bot() -> None:
    """Flujo principal del bot."""

    logger.info("=" * 60)
    logger.info("   POLYMARKET TRADING BOT")
    logger.info("=" * 60)

    # 1. Cargar configuración
    cfg = load_config()
    logger.info(
        "⚙️  Config cargada — simulation=%s, chain=%d",
        cfg.simulation_mode,
        cfg.auth.chain_id,
    )

    if cfg.simulation_mode:
        logger.info("🧪 MODO SIMULACIÓN activado — no se enviarán órdenes reales")

    # 1b. Configurar log handler del dashboard (logs en memoria + fichero)
    log_dir = _optional_env("LOG_DIR", "logs")
    dash_handler = DashboardLogHandler(bot_state, log_dir=log_dir)
    dash_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)s — %(message)s", datefmt="%H:%M:%S"
    ))
    logging.getLogger().addHandler(dash_handler)

    # 2. Autenticar contra el CLOB
    authenticator = Authenticator(cfg.auth)
    clob_client = authenticator.authenticate()

    # 3. Preparar credenciales WSS para el canal user
    wss_auth = None
    if cfg.auth.api_key and cfg.auth.api_secret and cfg.auth.api_passphrase:
        wss_auth = WSSAuth(
            apiKey=cfg.auth.api_key,
            secret=cfg.auth.api_secret,
            passphrase=cfg.auth.api_passphrase,
        )
    else:
        # Tras autenticar, las credenciales están en el cliente
        creds = clob_client.get_api_creds() if hasattr(clob_client, 'get_api_creds') else None
        if creds:
            wss_auth = WSSAuth(
                apiKey=creds.api_key,
                secret=creds.api_secret,
                passphrase=creds.api_passphrase,
            )

    # 4. Configurar WebSocket Manager
    ws_manager = WebSocketManager(
        ws_cfg=cfg.ws,
        on_market_event=on_market_event,
        on_user_event=on_user_event,
        auth=wss_auth,
    )

    # 5. Discovery: encontrar los mejores mercados automáticamente
    #    Si hay tokens manuales en .env, se usan esos.
    #    Si no, el discovery escanea y selecciona los mejores.
    token_ids = []
    condition_ids = []
    discovered_markets: list[MarketCandidate] = []  # Para el Trading Loop
    discovery: MarketDiscovery | None = None  # Se asigna solo en modo auto-discovery

    manual_tokens = []
    if cfg.market.token_id_yes:
        manual_tokens.append(cfg.market.token_id_yes)
    if cfg.market.token_id_no:
        manual_tokens.append(cfg.market.token_id_no)

    if manual_tokens:
        # Modo manual: usar los tokens configurados en .env
        token_ids = manual_tokens
        if cfg.market.condition_id:
            condition_ids = [cfg.market.condition_id]
        logger.info("📌 Modo manual — usando tokens de .env (%d tokens)", len(token_ids))
    else:
        # Modo automático: discovery
        logger.info("🔍 No hay tokens manuales en .env — activando Discovery...")
        discovery_cfg = DiscoveryConfig(
            clob_host=cfg.auth.clob_host,
            max_active_markets=int(
                _optional_env("MAX_ACTIVE_MARKETS", "5")
            ),
        )
        discovery = MarketDiscovery(discovery_cfg)

        try:
            ranked_markets = await discovery.scan()
            discovered_markets = ranked_markets  # Guardar para el Trading Loop

            for m in ranked_markets:
                if m.token_id_yes:
                    token_ids.append(m.token_id_yes)
                if m.token_id_no:
                    token_ids.append(m.token_id_no)
                if m.condition_id:
                    condition_ids.append(m.condition_id)

                # Mostrar desglose del score
                logger.info("\n%s", discovery.explain_score(m))

            if not token_ids and not condition_ids:
                logger.warning(
                    "⚠️  Discovery no encontró mercados válidos. "
                    "Configura tokens manualmente en .env o ajusta los filtros."
                )
        except Exception:
            logger.exception("❌ Error en Discovery")
        # No cerramos discovery aquí — lo reutilizaremos para el re-scan periódico

    # Registrar modo simulación y preguntas de mercados para las notificaciones de fills
    global _simulation_mode, _market_questions
    _simulation_mode = cfg.simulation_mode
    for m in discovered_markets:
        _market_questions[m.condition_id] = m.question

    # Notificar si se encontraron mercados en el discovery inicial
    if discovered_markets:
        await telegram.send_markets_found(discovered_markets)

    if token_ids:
        ws_manager.subscribe_market(token_ids)
        logger.info("📡 Suscrito a market channel — %d tokens", len(token_ids))

    if condition_ids:
        ws_manager.subscribe_user(condition_ids)
        logger.info(
            "📡 Suscrito a user channel — %d mercados", len(condition_ids)
        )

    # 6. Inicializar Risk Manager (Fase 4) + Trading Loop
    global trading_loop

    bankroll_usd = float(_optional_env("BANKROLL_USD", "1000"))

    risk_manager = RiskManager(
        bankroll_usd=bankroll_usd,
        kelly_fraction=cfg.risk.kelly_fraction,
        max_order_risk_pct=cfg.risk.max_bankroll_risk_pct,
        max_total_exposure_pct=float(_optional_env("MAX_TOTAL_EXPOSURE_PCT", "0.20")),
        max_session_loss_pct=float(_optional_env("MAX_SESSION_LOSS_PCT", "0.05")),
        max_consecutive_errors=int(_optional_env("MAX_CONSECUTIVE_ERRORS", "10")),
    )
    logger.info(
        "🛡️  Risk Manager — Kelly=%.2f | max_order=%.0f%% | max_exposure=%.0f%% | kill_loss=%.0f%%",
        cfg.risk.kelly_fraction,
        cfg.risk.max_bankroll_risk_pct * 100,
        float(_optional_env("MAX_TOTAL_EXPOSURE_PCT", "0.20")) * 100,
        float(_optional_env("MAX_SESSION_LOSS_PCT", "0.05")) * 100,
    )

    def _on_quote(market: MarketCandidate, pair: QuotePair) -> None:
        """Actualiza el dashboard con los precios reales de la quote generada."""
        bot_state.update_quote(
            market.condition_id,
            market.question,
            pair.bid.price,
            pair.ask.price,
            pair.mid_at_generation,
        )

    if discovered_markets:
        trading_loop = TradingLoop(
            markets=discovered_markets,
            orderbook=orderbook_tracker,
            clob_client=clob_client,
            simulation=cfg.simulation_mode,
            bankroll_usd=bankroll_usd,
            cycle_interval_ms=float(_optional_env("CYCLE_INTERVAL_MS", "500")),
            risk_manager=risk_manager,
            quote_callback=_on_quote,
        )
        logger.info(
            "🔄 Trading loop configurado — %d mercados, simulation=%s",
            len(discovered_markets),
            cfg.simulation_mode,
        )

    # 6b. Conectar BotState con los componentes del bot
    bot_state.wire(
        risk_manager=risk_manager,
        trading_loop=trading_loop,
        simulation=cfg.simulation_mode,
        bankroll=bankroll_usd,
    )
    # Las quotes del dashboard se actualizarán en el primer ciclo del trading loop
    # via quote_callback (usando los precios reales generados por el QuotingEngine).

    # 7. Lanzar WebSockets + Trading Loop con manejo de señal para cierre limpio
    loop = asyncio.get_running_loop()

    def _signal_handler():
        logger.info("🛑 Señal de cierre recibida...")
        if trading_loop:
            asyncio.create_task(trading_loop.stop())
        asyncio.create_task(ws_manager.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows no soporta add_signal_handler
            pass

    # Re-scan periódico del discovery (cada 5 minutos)
    _RESCAN_INTERVAL_SEC = int(_optional_env("DISCOVERY_RESCAN_SEC", "300"))

    async def _rescan_loop() -> None:
        """Re-escanea mercados periódicamente y actualiza el trading loop."""
        await asyncio.sleep(_RESCAN_INTERVAL_SEC)  # Primera espera antes del primer re-scan
        while True:
            try:
                logger.info("🔍 Re-scan periódico de mercados...")
                new_markets = await discovery.scan(force=True)
                if new_markets:
                    # Actualizar dict de preguntas para notificaciones de fills
                    for m in new_markets:
                        _market_questions[m.condition_id] = m.question
                    # Notificar si los mercados cambiaron respecto a los anteriores
                    prev_cids = {m.condition_id for m in (trading_loop._markets if trading_loop else [])}
                    new_cids = {m.condition_id for m in new_markets}
                    if new_cids != prev_cids:
                        await telegram.send_markets_found(new_markets)
                    if trading_loop:
                        trading_loop.update_markets(new_markets)
                    logger.info("✅ Re-scan completado — %d mercados activos", len(new_markets))
            except Exception:
                logger.exception("❌ Error en re-scan periódico")
            await asyncio.sleep(_RESCAN_INTERVAL_SEC)

    # Notificar arranque por Telegram
    await telegram.send_startup(
        simulation=cfg.simulation_mode,
        n_markets=len(discovered_markets),
        bankroll=bankroll_usd,
    )

    # Lanzar todo concurrentemente
    dashboard_port = int(_optional_env("DASHBOARD_PORT", "8080"))
    dashboard_server = DashboardServer(
        state=bot_state,
        port=dashboard_port,
        password=_optional_env("DASHBOARD_PASSWORD", ""),
    )
    tasks = [
        asyncio.create_task(ws_manager.start(), name="ws"),
        asyncio.create_task(dashboard_server.start(), name="dashboard"),
    ]
    if trading_loop:
        # Dar 2 segundos al WebSocket para conectar antes de empezar a cotizar
        async def _delayed_trading():
            logger.info("⏳ Esperando 2s para que el WebSocket se estabilice...")
            await asyncio.sleep(2.0)
            await trading_loop.run()
        tasks.append(asyncio.create_task(_delayed_trading(), name="trading"))

    if discovery:
        tasks.append(asyncio.create_task(_rescan_loop(), name="rescan"))

    logger.info("🚀 Bot iniciado — WebSocket + Trading Loop + Re-scan activos")
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if discovery:
            await discovery.close()


def main():
    """Entry point del script."""
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("👋 Bot detenido por el usuario.")
    except Exception:
        logger.exception("💥 Error fatal")
        sys.exit(1)


if __name__ == "__main__":
    main()

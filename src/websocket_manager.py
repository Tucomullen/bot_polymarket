"""
src/websocket_manager.py — Gestión de conexiones WebSocket a Polymarket.

Canales:
  - market : orderbook en tiempo real (libro de órdenes, cambios de precio)
  - user   : actualizaciones de cuenta (trades, órdenes ejecutadas)

Características:
  - Heartbeat PING cada 10 s para mantener la conexión abierta
  - Reconexión automática exponencial ante desconexiones
  - Callbacks asíncronos para procesar eventos sin bloqueo
  - Thread-safe para integración con el bucle principal del bot
"""

import asyncio
import json
import logging
import os
import ssl
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

import websockets
from websockets.asyncio.client import connect as ws_connect

from config.settings import WebSocketConfig

_VERIFY_SSL = os.getenv("VERIFY_SSL", "true").lower() != "false"
_SSL_CONTEXT: ssl.SSLContext | None = None
if not _VERIFY_SSL:
    _SSL_CONTEXT = ssl.create_default_context()
    _SSL_CONTEXT.check_hostname = False
    _SSL_CONTEXT.verify_mode = ssl.CERT_NONE

logger = logging.getLogger("polybot.ws")


# ---------------------------------------------------------------------------
# Tipos de eventos
# ---------------------------------------------------------------------------

class WSChannel(Enum):
    MARKET = "market"
    USER = "user"


class MarketEventType(Enum):
    BOOK = "book"
    PRICE_CHANGE = "price_change"
    LAST_TRADE_PRICE = "last_trade_price"
    TICK_SIZE_CHANGE = "tick_size_change"


class UserEventType(Enum):
    TRADE = "trade"
    ORDER = "order"


# Tipo para callbacks: reciben el dict del evento y no devuelven nada
EventCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


@dataclass
class WSSubscription:
    """Define una suscripción a un canal WebSocket."""
    channel: WSChannel
    # Para market: lista de asset_ids (token IDs)
    asset_ids: list[str] = field(default_factory=list)
    # Para user: lista de condition_ids (mercados)
    market_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Autenticación WSS (para canal user)
# ---------------------------------------------------------------------------

@dataclass
class WSSAuth:
    """Credenciales para suscripción autenticada al canal user."""
    apiKey: str
    secret: str
    passphrase: str


# ---------------------------------------------------------------------------
# Conexión WebSocket individual
# ---------------------------------------------------------------------------

class PolymarketWSConnection:
    """
    Gestiona una conexión WebSocket individual a un canal de Polymarket.
    Incluye heartbeat automático y reconexión exponencial.
    """

    HEARTBEAT_INTERVAL = 10  # segundos
    MAX_RECONNECT_DELAY = 60  # segundos
    INITIAL_RECONNECT_DELAY = 1  # segundo

    def __init__(
        self,
        url: str,
        channel: WSChannel,
        on_event: EventCallback,
        auth: WSSAuth | None = None,
    ):
        self._url = url
        self._channel = channel
        self._on_event = on_event
        self._auth = auth
        self._ws: Any = None
        self._running = False
        self._reconnect_delay = self.INITIAL_RECONNECT_DELAY
        self._subscriptions: list[WSSubscription] = []
        self._last_pong: float = 0.0

    def add_subscription(self, sub: WSSubscription) -> None:
        self._subscriptions.append(sub)

    async def start(self) -> None:
        """Inicia la conexión con reconexión automática."""
        self._running = True
        while self._running:
            try:
                await self._connect_and_listen()
            except (
                websockets.ConnectionClosed,
                websockets.InvalidURI,
                ConnectionRefusedError,
                OSError,
            ) as exc:
                if not self._running:
                    break
                logger.warning(
                    "⚠️  Desconexión en canal %s: %s — Reconectando en %ds...",
                    self._channel.value,
                    exc,
                    self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self.MAX_RECONNECT_DELAY
                )

    async def stop(self) -> None:
        """Cierra la conexión limpiamente."""
        self._running = False
        if self._ws:
            await self._ws.close()
            logger.info("🔌 Canal %s cerrado.", self._channel.value)

    async def _connect_and_listen(self) -> None:
        """Establece conexión, envía suscripciones y escucha mensajes."""
        logger.info("🔗 Conectando a %s (%s)...", self._url, self._channel.value)

        async with ws_connect(
            self._url,
            ping_interval=None,  # Gestionamos el heartbeat manualmente
            ping_timeout=None,
            close_timeout=5,
            max_size=2**20,  # 1 MB máx por mensaje
            ssl=_SSL_CONTEXT,
        ) as ws:
            self._ws = ws
            self._reconnect_delay = self.INITIAL_RECONNECT_DELAY
            logger.info("✅ Conectado a canal %s", self._channel.value)

            # Enviar suscripciones
            await self._send_subscriptions(ws)

            # Lanzar heartbeat en paralelo
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))

            try:
                async for raw_message in ws:
                    await self._handle_message(raw_message)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    async def _send_subscriptions(self, ws: Any) -> None:
        """Envía los mensajes de suscripción al WebSocket."""
        for sub in self._subscriptions:
            payload: dict[str, Any] = {"type": sub.channel.value}

            if sub.channel == WSChannel.MARKET:
                payload["assets_ids"] = sub.asset_ids
            elif sub.channel == WSChannel.USER:
                payload["markets"] = sub.market_ids
                if self._auth:
                    payload["auth"] = {
                        "apiKey": self._auth.apiKey,
                        "secret": self._auth.secret,
                        "passphrase": self._auth.passphrase,
                    }

            await ws.send(json.dumps(payload))
            logger.info(
                "📡 Suscripción enviada — canal=%s, payload_keys=%s",
                sub.channel.value,
                list(payload.keys()),
            )

    async def _heartbeat_loop(self, ws: Any) -> None:
        """
        Envía un PING cada HEARTBEAT_INTERVAL segundos.
        Polymarket requiere actividad periódica para no cerrar la conexión.
        """
        while self._running:
            try:
                pong_waiter = await ws.ping()
                await asyncio.wait_for(pong_waiter, timeout=5)
                self._last_pong = time.monotonic()
                logger.debug(
                    "💓 Heartbeat OK — canal %s", self._channel.value
                )
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                logger.warning(
                    "⚠️  Heartbeat sin respuesta en canal %s",
                    self._channel.value,
                )
                break
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)

    async def _handle_message(self, raw: str | bytes) -> None:
        """Parsea y despacha un mensaje entrante."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("⚠️  Mensaje no-JSON recibido: %s", raw[:200])
            return

        # Despachar al callback registrado
        try:
            await self._on_event(data)
        except Exception:
            logger.exception("❌ Error procesando evento en canal %s", self._channel.value)

        # Ceder control al event loop para que los timers (sleep) puedan disparar.
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Orquestador de WebSockets
# ---------------------------------------------------------------------------

class WebSocketManager:
    """
    Orquesta ambas conexiones WebSocket (market + user) y las ejecuta
    concurrentemente con asyncio.
    """

    def __init__(
        self,
        ws_cfg: WebSocketConfig,
        on_market_event: EventCallback,
        on_user_event: EventCallback,
        auth: WSSAuth | None = None,
    ):
        self._cfg = ws_cfg
        self._market_conn = PolymarketWSConnection(
            url=ws_cfg.market_url,
            channel=WSChannel.MARKET,
            on_event=on_market_event,
        )
        self._user_conn = PolymarketWSConnection(
            url=ws_cfg.user_url,
            channel=WSChannel.USER,
            on_event=on_user_event,
            auth=auth,
        )
        self._tasks: list[asyncio.Task] = []

    def subscribe_market(self, asset_ids: list[str]) -> None:
        """Suscribe a actualizaciones del libro de órdenes por token IDs."""
        self._market_conn.add_subscription(
            WSSubscription(channel=WSChannel.MARKET, asset_ids=asset_ids)
        )

    def subscribe_user(self, market_ids: list[str]) -> None:
        """Suscribe a actualizaciones de cuenta por condition IDs."""
        self._user_conn.add_subscription(
            WSSubscription(channel=WSChannel.USER, market_ids=market_ids)
        )

    async def start(self) -> None:
        """Lanza ambas conexiones concurrentemente."""
        logger.info("🚀 Iniciando WebSocket Manager...")
        self._tasks = [
            asyncio.create_task(
                self._market_conn.start(), name="ws-market"
            ),
            asyncio.create_task(
                self._user_conn.start(), name="ws-user"
            ),
        ]
        # Esperar a que terminen (solo terminan si se llama stop())
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        """Cierra ambas conexiones limpiamente."""
        logger.info("🛑 Deteniendo WebSocket Manager...")
        await self._market_conn.stop()
        await self._user_conn.stop()
        for task in self._tasks:
            task.cancel()
        logger.info("✅ WebSocket Manager detenido.")

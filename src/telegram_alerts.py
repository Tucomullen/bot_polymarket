"""
src/telegram_alerts.py — Alertas por Telegram para el bot de trading.

Envia notificaciones a un chat/canal de Telegram cuando ocurren eventos
importantes: arranque, kill switch, reinicios, resumen diario de P&L.

Configuracion (variables en .env):
  TELEGRAM_BOT_TOKEN  — token del bot creado con @BotFather
  TELEGRAM_CHAT_ID    — ID del chat/canal donde enviar (ej. -100123456789)

Si las variables no estan configuradas, las alertas se desactivan silenciosamente
(el bot sigue funcionando sin ellas).

Uso:
    alerter = TelegramAlerter.from_env()
    await alerter.send_startup(simulation=True)
    await alerter.send_kill_switch("perdida de sesion $51")
    await alerter.send_daily_summary(pnl=3.42, markets=5, uptime_h=8.0)
"""

import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("polybot.telegram")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramAlerter:
    """
    Cliente de alertas de Telegram.
    Todas las llamadas son async y fire-and-forget: no bloquean el bot.
    """

    def __init__(self, token: str = "", chat_id: str = ""):
        self._token = token.strip()
        self._chat_id = chat_id.strip()
        self._enabled = bool(self._token and self._chat_id)
        if not self._enabled:
            logger.info("Telegram alertas desactivadas (TELEGRAM_BOT_TOKEN/CHAT_ID no configurados)")

    @classmethod
    def from_env(cls) -> "TelegramAlerter":
        return cls(
            token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        )

    # ------------------------------------------------------------------
    # API publica
    # ------------------------------------------------------------------

    async def send(self, message: str) -> bool:
        """
        Envia un mensaje de texto. Devuelve True si se envio correctamente.
        Nunca lanza excepciones — fallo silencioso para no interrumpir el bot.
        """
        if not self._enabled:
            return False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    TELEGRAM_API.format(token=self._token),
                    json={
                        "chat_id": self._chat_id,
                        "text": message,
                        "parse_mode": "HTML",
                    },
                )
                if resp.status_code != 200:
                    logger.warning("Telegram API error %d: %s", resp.status_code, resp.text[:100])
                    return False
            return True
        except Exception as exc:
            logger.warning("Error enviando alerta Telegram: %s", exc)
            return False

    async def send_startup(self, simulation: bool, n_markets: int = 0, bankroll: float = 0.0) -> None:
        mode = "SIMULACION" if simulation else "LIVE"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        msg = (
            f"<b>Bot arrancado [{mode}]</b>\n"
            f"Hora: {now}\n"
            f"Mercados: {n_markets} | Bankroll: ${bankroll:.0f}"
        )
        await self.send(msg)

    async def send_kill_switch(self, reason: str) -> None:
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        msg = (
            f"<b>KILL SWITCH ACTIVADO</b>\n"
            f"Motivo: {reason}\n"
            f"Hora: {now}\n"
            f"Accion: todas las ordenes canceladas. Revision manual requerida."
        )
        await self.send(msg)

    async def send_daily_summary(
        self,
        pnl: float,
        n_markets: int,
        uptime_h: float,
        n_fills: int = 0,
    ) -> None:
        emoji = "+" if pnl >= 0 else "-"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        msg = (
            f"<b>Resumen diario — {today}</b>\n"
            f"P&L: <b>{pnl:+.4f} USD</b>\n"
            f"Fills: {n_fills} | Mercados: {n_markets}\n"
            f"Uptime: {uptime_h:.1f}h"
        )
        await self.send(msg)

    async def send_rescan(self, n_markets: int, added: int, removed: int) -> None:
        if added == 0 and removed == 0:
            return
        msg = (
            f"Re-scan de mercados completado\n"
            f"Mercados activos: {n_markets} (+{added} / -{removed})"
        )
        await self.send(msg)

    async def send_error(self, error: str) -> None:
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        msg = f"<b>Error critico</b>\n{error[:300]}\nHora: {now}"
        await self.send(msg)

#!/usr/bin/env python3
"""
deploy/market_watcher.py — Vigila mercados L1/L2 y notifica via Telegram
con boton inline para activar produccion directamente desde el movil.

Corre en Oracle como servicio systemd independiente del bot principal.
"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = APP_DIR / ".env"
load_dotenv(ENV_FILE)
sys.path.insert(0, str(APP_DIR))

from src.discovery import MarketDiscovery, DiscoveryConfig  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-20s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("polybot.watcher")

SCAN_INTERVAL = int(os.getenv("WATCHER_SCAN_SEC", "900"))  # 15 min
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", ""))
TG_BASE = f"https://api.telegram.org/bot{TG_TOKEN}"

# Parametros conservadores para el primer dia en produccion
# (identicos a go_live.sh para coherencia)
PROD_PARAMS = {
    "SIMULATION_MODE": "false",
    "BANKROLL_USD": "1000",
    "KELLY_FRACTION": "0.15",
    "MAX_BANKROLL_RISK_PCT": "0.02",
    "MAX_SESSION_LOSS_PCT": "0.03",
    "MAX_TOTAL_EXPOSURE_PCT": "0.20",
}


class MarketWatcher:

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._notified_cids: set[str] = set()   # evitar spam al re-detectar los mismos mercados
        self._go_live_done = False
        self._tg_offset = 0

    # ── Telegram helpers ────────────────────────────────────────────────

    async def _tg(self, method: str, payload: dict) -> dict:
        try:
            r = await self._client.post(f"{TG_BASE}/{method}", json=payload, timeout=35.0)
            return r.json()
        except Exception as exc:
            logger.warning("TG %s error: %s", method, exc)
            return {}

    async def _send(self, text: str, keyboard: dict | None = None) -> dict:
        payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
        if keyboard:
            payload["reply_markup"] = keyboard
        return await self._tg("sendMessage", payload)

    async def _answer_cb(self, cb_id: str, text: str) -> None:
        await self._tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})

    async def _edit_msg(self, chat_id: str, msg_id: int, text: str) -> None:
        await self._tg("editMessageText", {
            "chat_id": chat_id, "message_id": msg_id,
            "text": text, "parse_mode": "HTML",
        })

    # ── Discovery ───────────────────────────────────────────────────────

    async def _scan(self) -> tuple[list, list]:
        cfg = DiscoveryConfig(max_active_markets=5, min_price=0.10, max_price=0.90)
        d = MarketDiscovery(cfg)
        try:
            all_markets = await d.scan(force=True)
        finally:
            await d.close()
        l1_l2 = [m for m in all_markets if m.market_level in (1, 2)]
        return l1_l2, all_markets

    # ── Go-live ─────────────────────────────────────────────────────────

    async def _go_live(self) -> None:
        if self._go_live_done:
            await self._send("El bot ya esta en produccion.")
            return

        # Comprobar si ya estaba en live (por si el watcher se reinicio)
        current = ENV_FILE.read_text()
        if "SIMULATION_MODE=false" in current:
            await self._send("El bot ya estaba en SIMULATION_MODE=false.")
            self._go_live_done = True
            return

        logger.info("Iniciando go-live...")

        # 1. Backup del .env
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = Path(f"{ENV_FILE}.backup_{stamp}")
        shutil.copy2(ENV_FILE, backup)
        logger.info("Backup creado: %s", backup.name)

        # 2. Parchear .env con parametros de produccion
        content = current
        for key, val in PROD_PARAMS.items():
            pattern = rf"^{key}=.*$"
            replacement = f"{key}={val}"
            if re.search(pattern, content, re.MULTILINE):
                content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
            else:
                content += f"\n{key}={val}\n"
        ENV_FILE.write_text(content)
        logger.info(".env actualizado con parametros de produccion")

        # 3. Reiniciar el servicio del bot
        result = subprocess.run(
            ["sudo", "systemctl", "restart", "polymarket-bot"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.error("Error reiniciando: %s", result.stderr)
            await self._send(f"Error al reiniciar el servicio:\n<code>{result.stderr[:300]}</code>")
            return

        self._go_live_done = True
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        params_txt = "\n".join(f"{k}={v}" for k, v in PROD_PARAMS.items())
        await self._send(
            f"<b>BOT EN PRODUCCION</b> — {now}\n\n"
            f"Parametros aplicados:\n<code>{params_txt}</code>\n\n"
            f"Dashboard: http://129.213.115.16:8080\n\n"
            f"Para revertir:\n"
            f"<code>sudo sed -i 's/SIMULATION_MODE=false/SIMULATION_MODE=true/' {ENV_FILE}\n"
            f"sudo systemctl restart polymarket-bot</code>"
        )
        logger.info("Go-live completado correctamente")

    # ── Loop de escaneo ─────────────────────────────────────────────────

    async def _scan_loop(self):
        while True:
            try:
                logger.info("Escaneando mercados (L1/L2)...")
                l1_l2, all_markets = await self._scan()
                new = [m for m in l1_l2 if m.condition_id not in self._notified_cids]

                if new:
                    lines = [f"<b>Mercados L1/L2 disponibles ({len(l1_l2)})</b> — condiciones aptas para produccion\n"]
                    for m in l1_l2:
                        lvl = "L2-50%" if m.market_level == 2 else "L1"
                        lines.append(
                            f"• [{lvl}] <b>{m.question[:50]}</b>\n"
                            f"  spread={m.spread_cents:.1f}c | mid={m.midpoint:.2f} | score={m.score:.0f}"
                        )
                    lines.append("\nActiva produccion cuando hayas depositado USDC en Polymarket.")
                    kb = {"inline_keyboard": [[
                        {"text": "Activar PRODUCCION", "callback_data": "golive"},
                        {"text": "Ignorar", "callback_data": "skip"},
                    ]]}
                    await self._send("\n".join(lines), keyboard=kb)
                    self._notified_cids.update(m.condition_id for m in l1_l2)
                    logger.info("Notificacion enviada — %d mercados L1/L2", len(l1_l2))

                # Resetear para re-notificar cuando vuelvan L1/L2 tras un periodo sin ellos
                if not l1_l2:
                    self._notified_cids.clear()
                    logger.info("Sin L1/L2 — %d mercados totales (LR u otros)", len(all_markets))

            except Exception:
                logger.exception("Error en scan_loop")

            await asyncio.sleep(SCAN_INTERVAL)

    # ── Loop de polling Telegram ─────────────────────────────────────────

    async def _poll_loop(self):
        while True:
            try:
                resp = await self._tg("getUpdates", {
                    "offset": self._tg_offset,
                    "timeout": 30,
                    "allowed_updates": ["callback_query"],
                })
                for upd in resp.get("result", []):
                    self._tg_offset = upd["update_id"] + 1
                    await self._handle_update(upd)
            except Exception:
                logger.exception("Error en poll_loop")
                await asyncio.sleep(5)

    async def _handle_update(self, upd: dict) -> None:
        cq = upd.get("callback_query")
        if not cq:
            return

        # Solo aceptar del chat configurado
        if str(cq.get("from", {}).get("id", "")) != TG_CHAT_ID:
            await self._answer_cb(cq["id"], "No autorizado.")
            return

        data = cq.get("data", "")
        msg_id = cq.get("message", {}).get("message_id")
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))

        if data == "golive":
            await self._answer_cb(cq["id"], "Activando produccion...")
            if msg_id:
                await self._edit_msg(chat_id, msg_id, "Activando produccion...")
            await self._go_live()
        elif data == "skip":
            await self._answer_cb(cq["id"], "Ignorado.")
            if msg_id:
                await self._edit_msg(chat_id, msg_id, "Ignorado. El watcher sigue monitorizando.")

    # ── Entry point ──────────────────────────────────────────────────────

    async def run(self):
        if not TG_TOKEN or not TG_CHAT_ID:
            logger.error("Faltan TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID en .env")
            return
        logger.info("Market Watcher iniciado — scan cada %ds", SCAN_INTERVAL)
        async with httpx.AsyncClient() as client:
            self._client = client
            await self._send(
                f"<b>Market Watcher iniciado</b>\n"
                f"Escaneo cada {SCAN_INTERVAL // 60} min.\n"
                f"Aviso cuando haya mercados L1/L2 aptos para produccion."
            )
            await asyncio.gather(self._scan_loop(), self._poll_loop())


if __name__ == "__main__":
    asyncio.run(MarketWatcher().run())

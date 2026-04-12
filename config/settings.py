"""
config/settings.py — Carga centralizada de configuración desde variables de entorno.

Todas las claves sensibles se leen de .env (nunca hardcodeadas).
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Carga .env desde la raíz del proyecto
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)


def _require(name: str) -> str:
    """Lee una variable de entorno obligatoria o lanza error claro."""
    val = os.getenv(name, "").strip()
    if not val:
        raise EnvironmentError(
            f"⛔ Variable de entorno '{name}' no configurada. "
            f"Revisa tu archivo .env (copia .env.example como .env y rellena los valores)."
        )
    return val


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# ---------------------------------------------------------------------------
# Dataclasses de configuración
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuthConfig:
    """Credenciales de autenticación L1 y L2."""
    private_key: str
    funder_address: str
    signature_type: int
    chain_id: int
    clob_host: str

    # L2 — se rellenan tras la primera autenticación
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""


@dataclass(frozen=True)
class WebSocketConfig:
    """URLs de los canales WebSocket de Polymarket."""
    market_url: str
    user_url: str
    heartbeat_interval_sec: int = 10  # PING cada 10 s


@dataclass(frozen=True)
class MarketConfig:
    """Mercado objetivo y tokens."""
    token_id_yes: str
    token_id_no: str
    condition_id: str


@dataclass(frozen=True)
class RiskConfig:
    """Parámetros de gestión de riesgo."""
    max_bankroll_risk_pct: float
    max_spread_cents: int
    kelly_fraction: float


@dataclass(frozen=True)
class BotConfig:
    """Configuración raíz del bot."""
    auth: AuthConfig
    ws: WebSocketConfig
    market: MarketConfig
    risk: RiskConfig
    simulation_mode: bool


def load_config() -> BotConfig:
    """Construye la configuración completa leyendo de .env."""

    auth = AuthConfig(
        private_key=_require("PRIVATE_KEY"),
        funder_address=_optional("FUNDER_ADDRESS"),
        signature_type=int(_optional("SIGNATURE_TYPE", "0")),
        chain_id=int(_optional("CHAIN_ID", "137")),
        clob_host=_optional("CLOB_HOST", "https://clob.polymarket.com"),
        api_key=_optional("CLOB_API_KEY"),
        api_secret=_optional("CLOB_SECRET"),
        api_passphrase=_optional("CLOB_PASSPHRASE"),
    )

    ws = WebSocketConfig(
        market_url=_optional(
            "WS_MARKET_URL",
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        ),
        user_url=_optional(
            "WS_USER_URL",
            "wss://ws-subscriptions-clob.polymarket.com/ws/user",
        ),
    )

    market = MarketConfig(
        token_id_yes=_optional("TARGET_TOKEN_ID_YES"),
        token_id_no=_optional("TARGET_TOKEN_ID_NO"),
        condition_id=_optional("CONDITION_ID"),
    )

    risk = RiskConfig(
        max_bankroll_risk_pct=float(_optional("MAX_BANKROLL_RISK_PCT", "0.05")),
        max_spread_cents=int(_optional("MAX_SPREAD_CENTS", "5")),
        kelly_fraction=float(_optional("KELLY_FRACTION", "0.25")),
    )

    simulation = _optional("SIMULATION_MODE", "true").lower() == "true"

    return BotConfig(
        auth=auth,
        ws=ws,
        market=market,
        risk=risk,
        simulation_mode=simulation,
    )

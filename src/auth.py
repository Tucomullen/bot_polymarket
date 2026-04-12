"""
src/auth.py — Módulo de Autenticación L1 y L2 para Polymarket CLOB.

L1: Firma con clave privada para crear o derivar credenciales API.
L2: Uso de API Key / Secret / Passphrase para autenticar el cliente CLOB.

Flujo:
  1. Se instancia ClobClient con la clave privada (L1).
  2. Se llama a create_or_derive_api_creds() para obtener credenciales L2.
  3. Se persisten las credenciales L2 en .env para evitar regenerarlas.
  4. Se configura el cliente con set_api_creds() para operaciones autenticadas.
"""

import logging
from pathlib import Path

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

from config.settings import AuthConfig

logger = logging.getLogger("polybot.auth")


class Authenticator:
    """Gestiona la autenticación de dos niveles contra el CLOB de Polymarket."""

    def __init__(self, auth_cfg: AuthConfig):
        self._cfg = auth_cfg
        self._client: ClobClient | None = None

    # ------------------------------------------------------------------
    # Paso 1 — Construir cliente L1 (firma con clave privada)
    # ------------------------------------------------------------------
    def _build_l1_client(self) -> ClobClient:
        """
        Crea un ClobClient autenticado a nivel L1.
        - signature_type 0 → EOA directa (MetaMask, hardware wallet)
        - signature_type 1 → Magic / Email (wallet proxy)
        - signature_type 2 → Gnosis Safe
        """
        kwargs: dict = {
            "host": self._cfg.clob_host,
            "key": self._cfg.private_key,
            "chain_id": self._cfg.chain_id,
        }
        # Wallets proxy requieren funder y signature_type explícito
        if self._cfg.signature_type in (1, 2) and self._cfg.funder_address:
            kwargs["signature_type"] = self._cfg.signature_type
            kwargs["funder"] = self._cfg.funder_address

        client = ClobClient(**kwargs)
        logger.info(
            "✅ Cliente L1 creado — host=%s, chain=%d, sig_type=%d",
            self._cfg.clob_host,
            self._cfg.chain_id,
            self._cfg.signature_type,
        )
        return client

    # ------------------------------------------------------------------
    # Paso 2 — Obtener o derivar credenciales L2
    # ------------------------------------------------------------------
    def _get_or_create_l2_creds(self, client: ClobClient) -> ApiCreds:
        """
        Si ya tenemos credenciales L2 en la config (.env), las reutiliza.
        Si no, llama a create_or_derive_api_creds() y las persiste.
        """
        # ¿Ya tenemos credenciales L2?
        if (
            self._cfg.api_key
            and self._cfg.api_secret
            and self._cfg.api_passphrase
        ):
            creds = ApiCreds(
                api_key=self._cfg.api_key,
                api_secret=self._cfg.api_secret,
                api_passphrase=self._cfg.api_passphrase,
            )
            logger.info("🔑 Credenciales L2 cargadas desde .env")
            return creds

        # Derivar nuevas credenciales
        logger.info("🔐 Derivando credenciales L2 (primera ejecución)...")
        creds = client.create_or_derive_api_creds()

        # Persistir en .env para no volver a derivar
        self._persist_l2_creds(creds)
        logger.info("💾 Credenciales L2 guardadas en .env")

        return creds

    @staticmethod
    def _persist_l2_creds(creds: ApiCreds) -> None:
        """Escribe las credenciales L2 en el archivo .env local."""
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if not env_path.exists():
            env_path.touch()

        content = env_path.read_text()
        replacements = {
            "CLOB_API_KEY": creds.api_key,
            "CLOB_SECRET": creds.api_secret,
            "CLOB_PASSPHRASE": creds.api_passphrase,
        }

        for key, value in replacements.items():
            marker = f"{key}="
            if marker in content:
                # Reemplazar la línea existente
                lines = content.split("\n")
                content = "\n".join(
                    f"{key}={value}" if line.startswith(marker) else line
                    for line in lines
                )
            else:
                content += f"\n{key}={value}"

        env_path.write_text(content)

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------
    def authenticate(self) -> ClobClient:
        """
        Ejecuta el flujo completo de autenticación L1 → L2 y devuelve
        un ClobClient totalmente autenticado, listo para operar.
        """
        # Verificación de conexión básica (L0)
        l0_client = ClobClient(self._cfg.clob_host)
        try:
            ok = l0_client.get_ok()
            server_time = l0_client.get_server_time()
            logger.info("🌐 CLOB alcanzable — ok=%s, server_time=%s", ok, server_time)
        except Exception as exc:
            logger.error("❌ No se pudo contactar con el CLOB: %s", exc)
            raise ConnectionError(
                f"No se pudo conectar a {self._cfg.clob_host}. "
                "Verifica tu conexión de red y el endpoint."
            ) from exc

        # L1 — Firma con clave privada
        client = self._build_l1_client()

        # L2 — Credenciales API
        creds = self._get_or_create_l2_creds(client)
        client.set_api_creds(creds)
        logger.info("✅ Autenticación L1+L2 completada con éxito")

        self._client = client
        return client

    @property
    def client(self) -> ClobClient:
        if self._client is None:
            raise RuntimeError("Llama a authenticate() antes de acceder al cliente.")
        return self._client

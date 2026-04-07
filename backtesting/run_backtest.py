"""
backtesting/run_backtest.py — Script principal del backtesting.

Uso:
    python backtesting/run_backtest.py
    python backtesting/run_backtest.py --days 60 --markets 5
    python backtesting/run_backtest.py --days 30 --no-report
    python backtesting/run_backtest.py --condition-ids 0xabc 0xdef  # mercados específicos

El script:
  1. Descarga los N mercados más activos (o los indicados) de la Gamma API
  2. Simula la estrategia de market making sobre datos históricos
  3. Calcula métricas: Sharpe, drawdown, fill rate, días rentables
  4. Genera informe HTML en backtesting/report_YYYY-MM-DD.html
  5. Imprime veredicto en consola (PASS / FAIL según umbrales del plan)
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Asegurar que el directorio raíz del proyecto está en el path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Cargar .env si existe
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configurar variables mínimas de entorno antes de importar src/
os.environ.setdefault("PRIVATE_KEY", "0" * 64)
os.environ.setdefault("VERIFY_SSL", "true")
os.environ.setdefault("SIMULATION_MODE", "true")

from backtesting.downloader import download_histories, download_market_history, fetch_top_markets
from backtesting.simulator import run_backtest
from backtesting.metrics import calculate_metrics, print_summary
from backtesting.report import generate_report
from src.quoting import QuotingConfig

# Logging básico
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("polybot.backtest")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest del bot de market making en Polymarket"
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Días de historia a descargar (default: 30)",
    )
    parser.add_argument(
        "--markets", type=int, default=5,
        help="Número de mercados top a testear (default: 5)",
    )
    parser.add_argument(
        "--bankroll", type=float, default=1000.0,
        help="Bankroll simulado en USD (default: 1000)",
    )
    parser.add_argument(
        "--base-spread", type=float, default=2.0,
        help="Spread base en centavos para el QuotingEngine (default: 2.0)",
    )
    parser.add_argument(
        "--no-report", action="store_true",
        help="No generar informe HTML",
    )
    parser.add_argument(
        "--no-ssl-verify", action="store_true",
        help="Deshabilitar verificación SSL (solo para proxies corporativos)",
    )
    parser.add_argument(
        "--condition-ids", nargs="+", default=[],
        metavar="ID",
        help="Simular mercados específicos por conditionId en lugar del top-N",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    """
    Función principal async. Devuelve 0 si la estrategia pasa todos los umbrales.
    """
    verify_ssl = not args.no_ssl_verify and os.getenv("VERIFY_SSL", "true").lower() == "true"
    logger.info("🚀 Iniciando backtest | días=%d | mercados=%d | bankroll=$%.0f | SSL=%s",
                args.days, args.markets, args.bankroll, verify_ssl)

    # 1. Descargar historial de precios
    if args.condition_ids:
        # Mercados específicos: primero buscar sus metadatos en la API
        logger.info("🔍 Buscando metadatos para %d condition IDs...", len(args.condition_ids))
        top = await fetch_top_markets(n=50, verify_ssl=verify_ssl)
        selected = [m for m in top if
                    (m.get("conditionId") or m.get("condition_id", "")) in args.condition_ids]
        if not selected:
            logger.error("❌ No se encontraron los conditionIds indicados en los top-50 mercados")
            return 1
        histories = []
        for raw in selected:
            h = await download_market_history(raw, args.days, verify_ssl)
            if h:
                histories.append(h)
    else:
        histories = await download_histories(
            n_markets=args.markets,
            days=args.days,
            verify_ssl=verify_ssl,
        )

    if not histories:
        logger.error("❌ No se pudieron descargar datos históricos. "
                     "Verifica conexión y que los mercados tengan historial.")
        return 1

    logger.info("📈 %d mercados con datos. Iniciando simulación...", len(histories))

    # 2. Simular
    quoting_cfg = QuotingConfig(base_spread_cents=args.base_spread)
    results = run_backtest(
        histories,
        bankroll_usd=args.bankroll,
        quoting_cfg=quoting_cfg,
    )

    # 3. Calcular métricas
    all_metrics = [calculate_metrics(r) for r in results]

    # 4. Imprimir resumen
    print_summary(all_metrics, args.bankroll, args.days)

    # 5. Generar informe HTML
    if not args.no_report:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        output = Path(__file__).parent / f"report_{ts}.html"
        report_path = generate_report(results, all_metrics, args.bankroll, args.days, output)
        print(f"  Informe guardado en: {report_path}\n")

    # 6. Exit code: 0 si pasa, 1 si falla (útil para CI)
    n_pass = sum(1 for m in all_metrics if m.passes)
    threshold = len(all_metrics) * 0.6
    return 0 if n_pass >= threshold else 1


def main() -> None:
    args = parse_args()
    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

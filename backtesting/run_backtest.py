"""
backtesting/run_backtest.py — Script principal del backtesting.

Uso:
    python backtesting/run_backtest.py                          # Discovery scoring (default)
    python backtesting/run_backtest.py --days 60 --markets 5
    python backtesting/run_backtest.py --no-discovery           # ranking bruto por volumen
    python backtesting/run_backtest.py --days 30 --no-report
    python backtesting/run_backtest.py --condition-ids 0xabc 0xdef

El script:
  1. Selecciona mercados via Discovery (scoring 10 factores) o top-N por volumen (--no-discovery)
  2. Simula la estrategia de market making sobre datos históricos
  3. Calcula métricas: Sharpe, drawdown, fill rate, días rentables
  4. Genera informe HTML en backtesting/report_YYYY-MM-DD_HHMM.html
  5. Imprime veredicto en consola (PASS / FAIL según umbrales del plan)
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Asegurarse de que la raíz del proyecto está en el path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from backtesting.downloader import (
    download_histories,
    download_histories_with_discovery,
    download_market_history,
    fetch_top_markets,
)
from backtesting.simulator import run_backtest
from backtesting.metrics import calculate_metrics, aggregate_metrics, print_summary
from backtesting.report import generate_report
from src.quoting import QuotingConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(name)-22s │ %(levelname)-5s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("polybot.backtest")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest de la estrategia de market making en Polymarket"
    )
    parser.add_argument("--days", type=int, default=30,
                        help="Período histórico a analizar (default: 30)")
    parser.add_argument("--markets", type=int, default=5,
                        help="Número de mercados top a testear (default: 5)")
    parser.add_argument("--bankroll", type=float, default=1000.0,
                        help="Capital simulado en USD (default: 1000)")
    parser.add_argument("--base-spread", type=float, default=2.0,
                        help="Spread base en centavos (default: 2.0)")
    parser.add_argument("--condition-ids", nargs="+", default=[],
                        help="Condition IDs específicos a testear (en lugar del top-N)")
    parser.add_argument("--no-report", action="store_true",
                        help="No generar informe HTML")
    parser.add_argument("--no-ssl-verify", action="store_true",
                        help="Desactivar verificación SSL (proxy corporativo)")
    parser.add_argument("--no-discovery", action="store_true",
                        help="Usar ranking bruto por volumen en lugar del Discovery scoring")
    parser.add_argument("--min-price", type=float, default=0.15,
                        help="Precio mínimo para filtrar mercados direccionales (default: 0.15)")
    parser.add_argument("--max-price", type=float, default=0.85,
                        help="Precio máximo para filtrar mercados direccionales (default: 0.85)")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    """Función principal async del backtest."""
    verify_ssl = not args.no_ssl_verify

    logger.info("=" * 60)
    logger.info("   BACKTEST — Polymarket Trading Bot")
    logger.info("   Período: %d días | Bankroll: $%.0f | Spread: %.1f¢",
                args.days, args.bankroll, args.base_spread)
    logger.info("=" * 60)

    # 1. Descargar datos
    if args.condition_ids:
        logger.info("📌 Modo manual — %d condition IDs especificados", len(args.condition_ids))
        # Para condition IDs manuales, necesitamos buscar los mercados en la API
        top_raw = await fetch_top_markets(n=50, verify_ssl=verify_ssl)
        raw_by_cid = {m.get("conditionId", ""): m for m in top_raw}
        selected_raw = [raw_by_cid[cid] for cid in args.condition_ids if cid in raw_by_cid]
        if not selected_raw:
            logger.error("❌ No se encontraron los condition IDs especificados en la Gamma API")
            return 1

        import asyncio as _asyncio
        tasks = [download_market_history(m, args.days, verify_ssl) for m in selected_raw]
        results_raw = await _asyncio.gather(*tasks, return_exceptions=True)
        histories = [r for r in results_raw if not isinstance(r, Exception) and r is not None]
    elif args.no_discovery:
        logger.info("🔍 Modo automático (volumen) — descargando top-%d mercados", args.markets)
        histories = await download_histories(
            n_markets=args.markets,
            days=args.days,
            verify_ssl=verify_ssl,
        )
    else:
        logger.info(
            "🎯 Modo Discovery — selección por scoring de 10 factores (top-%d, precio %.2f–%.2f)",
            args.markets, args.min_price, args.max_price,
        )
        histories = await download_histories_with_discovery(
            n_markets=args.markets,
            days=args.days,
            verify_ssl=verify_ssl,
            min_price=args.min_price,
            max_price=args.max_price,
        )

    if not histories:
        logger.error("❌ No se pudieron descargar datos de ningún mercado")
        return 1

    logger.info("📊 %d mercados con datos válidos para el backtest", len(histories))

    # 2. Configurar QuotingEngine
    quoting_cfg = QuotingConfig(
        base_spread_cents=args.base_spread,
    )

    # 3. Simular
    logger.info("🔄 Ejecutando simulación...")
    backtest_results = run_backtest(
        histories=histories,
        bankroll_usd=args.bankroll,
        quoting_cfg=quoting_cfg,
    )

    # 4. Calcular métricas
    all_metrics = [calculate_metrics(r) for r in backtest_results]

    # 5. Imprimir resumen
    print_summary(all_metrics, args.bankroll, args.days)

    # 6. Generar informe HTML
    if not args.no_report:
        report_path = generate_report(
            results=backtest_results,
            metrics=all_metrics,
            bankroll=args.bankroll,
            days=args.days,
        )
        logger.info("📄 Informe guardado en: %s", report_path)

    # 7. Retornar código de salida (0 = PASS, 1 = FAIL)
    agg = aggregate_metrics(all_metrics)
    verdict = agg.get("verdict", "FAIL")
    return 0 if verdict == "PASS" else 1


def main() -> None:
    args = _parse_args()
    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

# Estado actual del proyecto — Polymarket Bot

## Qué es
Bot de market making para Polymarket CLOB (mercados de predicción binarios). Captura el spread bid/ask como maker, con Kelly fraccional para sizing y kill switch automático.

- **Repo:** https://github.com/Tucomullen/bot_polymarket.git
- **Local:** `c:\Users\Lgarc\Proyectos\polymarket-bot`
- **Servidor Oracle:** `ubuntu@129.213.115.16` (SSH key: `C:\Users\Lgarc\Proyectos\.ssh\ssh-key-2026-04-12.key`)
- **Dashboard en vivo:** http://129.213.115.16:8080

---

## Estado de fases

| Fase | Descripción | Estado |
|------|-------------|--------|
| 1 | Auth L1/L2, WebSocket, OrderbookTracker, Discovery | ✅ |
| 2 | QuotingEngine, OrderManager, TradingLoop | ✅ |
| SSL | Bypass SSL para proxy corporativo | ✅ |
| 3 | Fee-rate real, re-scan periódico cada 300s | ✅ |
| 4 | RiskManager: kill switch, Kelly fraccional, P&L tracking | ✅ |
| 5a | Unit tests: 40 tests (test_quoting.py + test_risk_manager.py) | ✅ |
| 5b | Backtesting: 5 módulos + integración MarketDiscovery | ✅ |
| 5c | Dashboard FastAPI SSE + Telegram alerts | ✅ |
| 5d | Deploy Oracle Cloud + paper trading | ✅ ACTIVO |
| Producción | SIMULATION_MODE=false, capital real | ⏳ tras 7-14d paper trading |

---

## Arquitectura de archivos

```
polymarket-bot/
├── main.py                    # Entry point: auth→discovery→WS→TradingLoop→Dashboard
├── config/settings.py         # Dataclass config desde .env
├── src/
│   ├── auth.py                # Auth L1 (private key) + L2 (API key/secret/passphrase)
│   ├── discovery.py           # Gamma API scan, scoring 10 factores, MarketCandidate
│   ├── orderbook.py           # Thread-safe OrderbookTracker
│   ├── quoting.py             # QuotingEngine: spread dinámico, inventory skew, Kelly
│   ├── risk_manager.py        # Kill switch, exposure tracking, P&L por fill
│   ├── order_manager.py       # sync_quotes(), fee-rate cache, paper trading mode
│   ├── trading_loop.py        # Loop 500ms/ciclo
│   ├── dashboard.py           # FastAPI SSE: /api/status /api/stream /api/logs /api/kill-switch
│   └── telegram_alerts.py     # Alertas: startup, kill switch, daily summary
├── backtesting/
│   ├── downloader.py          # Descarga CLOB, caché, download_histories_with_discovery()
│   ├── simulator.py           # Simula market making sobre datos históricos
│   ├── metrics.py             # Sharpe, drawdown, fill-rate, días rentables
│   ├── report.py              # Informe HTML/SVG
│   └── run_backtest.py        # CLI: --days --markets --no-discovery --min-price --max-price
├── tests/
│   ├── conftest.py
│   ├── test_quoting.py        # 17 tests
│   └── test_risk_manager.py   # 23 tests
└── deploy/
    ├── bot.service            # systemd unit
    ├── setup_oracle.sh        # Instalación automática en Ubuntu
    └── update.sh              # git pull + pip install + restart
```

---

## Paper trading — checklist en curso (iniciado 2026-04-12 ~21:42 UTC)

| Item | Estado | Verificar |
|------|--------|-----------|
| Re-scan cada 5min | ✅ verificado | — |
| Auto-restart tras crash | ✅ verificado | — |
| Kill switch via dashboard | ✅ verificado | — |
| Dashboard coherente con logs | ✅ verificado | — |
| Telegram configurado | ✅ HTTP 200 | chat_id=6309097851 |
| **72h sin crash** | ⏳ | 2026-04-15 ~21:42 UTC |
| **7 días sin memory leak** | ⏳ | 2026-04-19 |
| P&L simulado coherente | ⏳ | necesita fills |

---

## Backtesting — notas clave

- **Por defecto usa Discovery** (scoring 10 factores, mismo que el bot en live). `--no-discovery` para ranking bruto por volumen.
- **Filtro de precio:** `--min-price 0.15 --max-price 0.85` para descartar outrights direccionales.
- **FAIL esperado en abril 2026:** mercados dominados por NBA Finals/FIFA 2026 que no oscilan. El Spurs (~50%) mostró Sharpe=6.01 — la estrategia funciona cuando hay mercados cerca de 0.50.
- **La granularidad horaria subestima los fills reales** (el CLOB real tiene trades sub-minuto).

### Criterios de PASS por mercado
```
PnL > 0
Sharpe ratio >= 0.5
Fill rate >= 5%
Días rentables >= 60%
VEREDICTO PASS: ≥60% de mercados superan todos los umbrales
```

---

## Fixes de API aplicados (downloader.py)

- `clobTokenIds` llega como JSON string desde Gamma API → `json.loads()` antes de usar
- Endpoint de precio histórico: `clob.polymarket.com/prices-history` (NO gamma-api)
- Parámetro interval: `1m` para ≤30 días, `max` para >30 días — NO usar `startTs`/`endTs`

---

## Fórmulas clave

```python
# Kelly sizing
edge = (half_spread_cents / 100) / mid_price
kelly_size = bankroll * kelly_fraction * edge * 2
# Limitar por MAX_BANKROLL_RISK_PCT y exposición disponible

# Kill switch
killed = (session_pnl < -(bankroll * max_session_loss_pct)
          or consecutive_errors >= max_consecutive_errors)

# Scoring: 10 factores ponderados (suman 1.0)
# category=15%, maker_rebates=15%, liquidity_rewards=12%, spread=12%,
# competition=12%, volume=10%, price_centrality=6%, trade_frequency=6%,
# time_to_resolution=6%, event_risk=6%
```

---

## Comandos habituales

```bash
# Ver estado del bot en el servidor
curl http://129.213.115.16:8080/api/status

# Logs en tiempo real
ssh -i "C:\Users\Lgarc\Proyectos\.ssh\ssh-key-2026-04-12.key" ubuntu@129.213.115.16 \
  "sudo journalctl -u polymarket-bot -f"

# Memoria del proceso (check memory leak)
ssh -i "C:\Users\Lgarc\Proyectos\.ssh\ssh-key-2026-04-12.key" ubuntu@129.213.115.16 \
  "ps aux | grep main.py"

# Reiniciar el bot
ssh -i "C:\Users\Lgarc\Proyectos\.ssh\ssh-key-2026-04-12.key" ubuntu@129.213.115.16 \
  "sudo systemctl restart polymarket-bot"

# Ejecutar backtest local
python backtesting/run_backtest.py --days 30 --markets 5

# Correr tests
pytest tests/ -v
```

---

## Para ir a producción (tras completar 7-14 días de paper trading)

1. Verificar todo el checklist del punto anterior
2. Depositar USDC en Polymarket (cuenta Polygon)
3. En el servidor: `nano /home/ubuntu/bot/.env` → `SIMULATION_MODE=false`
4. `sudo systemctl restart polymarket-bot`
5. Verificar en logs: `simulation=False` y SIN "MODO SIMULACIÓN"

---

*Última actualización: 2026-04-12 — Fase 5d completada, paper trading activo*

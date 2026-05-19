# Synk — Geopolitical Event-Driven Trading System

> Paper-trading research project: a three-gate strategy that only enters
> equity / haven positions when geopolitical risk, momentum, and news
> sentiment all agree.

**Status:** Paper trading via Alpaca, in evaluation since 2026-04-22 (3-month review window). Not deployed to live capital.

![Status](https://img.shields.io/badge/status-paper--trading-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)

---

## Strategy

Three independent signal gates must all pass for entry. Any failure exits the position.

1. **Regime gate** — daily Geopolitical Risk Index (Caldara & Iacoviello) z-score classified into `NORMAL` / `ELEVATED` / `HIGH` / `EXTREME`. Equity longs require `NORMAL` or `ELEVATED`; havens activate on `HIGH`+.
2. **Momentum gate** — 20-day ROC > 0 *and* close > SMA(20). Filters counter-trend entries even when regime and sentiment agree.
3. **Sentiment gate** — FinBERT over GDELT 2.0 headlines, requiring dominant-class probability > 0.6 *and* |signed score| > 0.3.

All three gates are evaluated hourly. Entries are sized at Quarter-Kelly, capped per instrument.

## Architecture

```
                          ┌─────────────────────┐
                          │   APScheduler loop  │
                          └──────────┬──────────┘
                                     │ hourly
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
       ┌────────────┐         ┌────────────┐         ┌────────────┐
       │ GPR loader │         │   GDELT    │         │ Price feed │
       │  (.xls)    │         │  + FinBERT │         │  (Alpaca)  │
       └──────┬─────┘         └──────┬─────┘         └──────┬─────┘
              │                      │                      │
              ▼                      ▼                      ▼
        ┌─────────────────────────────────────────────────────────┐
        │  Strategy orchestrator  (regime ∧ momentum ∧ sentiment) │
        └──────────────────────────┬──────────────────────────────┘
                                   ▼
                          ┌────────────────┐       ┌──────────────┐
                          │  Kill switch   │──────▶│  Telegram    │
                          │  (2/5/30%)     │       │  alerts      │
                          └────────┬───────┘       └──────────────┘
                                   ▼
                          ┌────────────────┐
                          │  Order exec    │
                          │  (Alpaca paper)│
                          └────────────────┘

       Independent process: alerts/watchdog.py runs every 15 min via
       Task Scheduler, checks heartbeat freshness, re-alerts on halts.
```

## Signal gates

| Gate | Source | Pass condition | Module |
|---|---|---|---|
| Regime | Caldara–Iacoviello GPR daily | z-score classification by sleeve | `signals/regime_filter.py` |
| Momentum | Alpaca OHLCV | ROC(20) > 0 ∧ close > SMA(20) | `signals/momentum.py` |
| Sentiment | GDELT 2.0 headlines → FinBERT | max(prob) > 0.6 ∧ |score| > 0.3 | `signals/sentiment.py` |

Sentiment is pre-computed in a daily batch (`signals/finbert_drift_monitor.py`) and cached to `logs/sentiment_cache.jsonl` so the hot path doesn't pay FinBERT warm-load cost.

## Instruments & sizing

- **Equity sleeve:** SPY, QQQ
- **Haven sleeve:** GLD, FXY, TLT
- **Sizing:** Quarter-Kelly, capped per instrument (see `strategy/synk_strategy.py`)

## Risk management

Three hard limits in `risk/kill_switch.py`. All three trigger a halt + Telegram alert and require **manual recovery** (no auto-reset):

| Limit | Threshold |
|---|---|
| Per-trade stop | 2% |
| Daily loss cap | 5% |
| Peak drawdown cap | 30% |

State persists to `logs/kill_switch_state.json` so a process restart can't paper over a tripped halt.

## Stack

Python 3.11+, Alpaca paper API, Hugging Face `transformers` (FinBERT), GDELT 2.0, Caldara–Iacoviello GPR daily series, APScheduler, Lumibot (backtesting only).

## Setup

```bash
git clone https://github.com/Vanz23-23/Synk.git
cd Synk
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt

# Copy the env template and fill in your *paper* keys
cp .env.example .env   # (Windows: copy .env.example .env)

# Verify the Alpaca + Telegram connections
python test_connection.py
```

The GPR daily series is not checked in — it's redownloaded automatically by the bot's 16:30 ET job. To prime the cache immediately on a fresh clone:

```bash
python -c "from signals.regime_filter import download_gpr_daily; download_gpr_daily()"
```

### Windows: scheduled tasks

```powershell
powershell -ExecutionPolicy Bypass -File setup_tasks.ps1
```

Registers three Task Scheduler jobs:

- `SynkBot` — runs `run_bot.bat` at logon (auto-restart loop)
- `SynkWatchdog` — runs `alerts/watchdog.py` every 15 minutes
- `SynkWeeklyReview` — runs `weekly_review.py` every Sunday 19:00

### macOS / Linux

Equivalent `launchd` / `systemd` units are not yet shipped — see [What's intentionally not here](#whats-intentionally-not-here). The Python itself is portable; only the scheduler glue is Windows-only.

## What's intentionally not here

- **Backtest result files** (HTML tearsheets, parquet exports, CSV trade logs). Re-run `python backtest/synk_backtest.py` to regenerate. Headline performance numbers are not the point of this repo.
- **Live trading.** The strategy has only run paper since 2026-04-22 and is inside its 3-month evaluation window.
- **Cross-platform scheduler scripts.** Coming with the macOS migration.

## Disclaimer

Not financial advice. Paper trading only. Educational / portfolio project. The author makes no claim of profitability; the system is under evaluation and has known limitations:

- Single broker (Alpaca paper); execution model unverified against a second venue
- Limited universe (5 tickers)
- Sentiment-model drift not formally monitored beyond the daily cache
- No transaction-cost stress beyond Lumibot defaults
- GPR series is daily and lags real geopolitical events by hours-to-days

## License

MIT — see [LICENSE](LICENSE).

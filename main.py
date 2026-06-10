"""
main

Entry point for the Synk trading bot. Runs an APScheduler job loop that
coordinates data refresh, risk monitoring, and strategy evaluation.

Jobs (all UTC unless noted):
    Every  5 min:    write logs/heartbeat.json (watchdog liveness signal)
    Every 15 min:    check_triggers() — kill switch risk limits
    Hourly at :00:   fetch GDELT headlines -> FinBERT sentiment -> append cache
    Hourly at :00:   get_prices() — respects 24h cache, no-op if fresh
    Hourly at :05:   check_health() — append to logs/health.jsonl
    Hourly at :10:   evaluate_all_symbols() — gate evaluation + order submission
    Hourly at :10:   check_exits() — stop loss + momentum flip exit check
    Daily  08:00:    run_batch_check() — FinBERT drift monitor, append to health.jsonl
    Daily  16:30 ET: download_gpr_daily() — skip if < 24h old
    Daily  15:00:    rebalance_sleeve() — defence-beta sleeve (no-op unless enabled;
                     self-gates on quarterly cadence + drift band + market-open)
    Daily  21:00:    job_daily_summary() — one Telegram with the day's activity

Watchdog is a SEPARATE Task Scheduler process (alerts/watchdog.py), not a
thread here. This process writes heartbeat.json so watchdog detects hangs.

Usage:
    python main.py
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# ---------------------------------------------------------------------------
# Paths and sys.path
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent  # synk/ root

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from alerts.telegram_util import send_telegram  # noqa: E402

_LOG_DIR = _HERE / "logs"
_HEARTBEAT_PATH = _LOG_DIR / "heartbeat.json"

# ---------------------------------------------------------------------------
# Logging — stdout + logs/process.log, UTC timestamps
# ---------------------------------------------------------------------------
def _setup_logging() -> logging.Logger:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("main")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s UTC | %(levelname)s | %(message)s")
    fmt.converter = time.gmtime

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    fh = logging.FileHandler(_LOG_DIR / "process.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


log = _setup_logging()


# ---------------------------------------------------------------------------
# Heartbeat — written atomically so watchdog never reads a partial file
# ---------------------------------------------------------------------------
def job_heartbeat() -> None:
    """Write logs/heartbeat.json. Watchdog alerts if this goes stale."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _HEARTBEAT_PATH.with_suffix(".tmp")
    payload = {
        "last_alive": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pid": os.getpid(),
    }
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, _HEARTBEAT_PATH)
    log.debug("Heartbeat written")


# ---------------------------------------------------------------------------
# Kill switch check
# ---------------------------------------------------------------------------
def job_kill_switch_check() -> None:
    """Evaluate risk triggers against live Alpaca account. Halts on breach."""
    from risk.kill_switch import check_triggers  # noqa: PLC0415
    try:
        status = check_triggers()
        if status.triggered:
            log.critical("Kill switch triggered: %s", status.reason)
        else:
            log.info(
                "Kill switch OK | equity=%.2f | daily_pnl=%.2f%% | drawdown=%.2f%%",
                status.equity, status.daily_pnl_pct * 100, status.drawdown_pct * 100,
            )
    except Exception as exc:
        log.error("Kill switch check failed: %s", exc)


# ---------------------------------------------------------------------------
# Data refresh jobs
# ---------------------------------------------------------------------------
def job_sentiment() -> None:
    """Fetch GDELT headlines, run FinBERT, append result to sentiment_cache.jsonl."""
    from signals.sentiment import run_sentiment_cycle, append_to_jsonl  # noqa: PLC0415
    try:
        sig = run_sentiment_cycle()
        append_to_jsonl(sig)
        log.info(
            "Sentiment done | class=%s prob=%.3f score=%+.3f gate=%s n=%d",
            sig.dominant_class, sig.dominant_prob, sig.sentiment_score,
            "OPEN" if sig.signal else "CLOSED", sig.headline_count,
        )
    except Exception as exc:
        log.error("Sentiment job failed: %s", exc)


def job_prices() -> None:
    """Refresh OHLCV cache for all symbols (no-op if bars are < 24h old)."""
    from data.price_feed import get_prices, SYMBOLS  # noqa: PLC0415
    try:
        prices = get_prices(SYMBOLS)
        loaded = [sym for sym, df in prices.items() if not df.empty]
        log.info("Price refresh done | %d symbols loaded: %s", len(loaded), loaded)
    except Exception as exc:
        log.error("Price refresh failed: %s", exc)


def job_gpr_refresh() -> None:
    """Download fresh GPR daily XLS. Skips automatically if < 24h old."""
    from signals.regime_filter import download_gpr_daily, _DEFAULT_GPR_PATH  # noqa: PLC0415
    try:
        download_gpr_daily(_DEFAULT_GPR_PATH)
    except Exception as exc:
        log.error("GPR refresh failed: %s", exc)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
def job_health_check() -> None:
    """Run file-mtime health checks and append result to logs/health.jsonl."""
    from alerts.health_monitor import check_health, append_to_jsonl  # noqa: PLC0415
    try:
        status = check_health()
        append_to_jsonl(status)
        if not status.all_healthy:
            log.warning("Health issues detected: %s", status.issues)
    except Exception as exc:
        log.error("Health check failed: %s", exc)


# ---------------------------------------------------------------------------
# Strategy evaluation
# ---------------------------------------------------------------------------
def job_strategy() -> None:
    """
    Evaluate all gates for every symbol and submit bracket orders for open gates.
    Detects bracket exits since last run, records cooldown, blocks re-entry.
    Skips entirely if kill switch is HALTED.
    """
    from risk.kill_switch import kill_switch_active  # noqa: PLC0415
    from strategy.synk_strategy import (  # noqa: PLC0415
        evaluate_all_symbols,
        build_trade_instruction,
        load_exit_state,
        record_exit,
        is_in_cooldown,
        load_expected_positions,
        save_expected_positions,
    )
    from execution.order_executor import submit_entry  # noqa: PLC0415
    from config import get_config  # noqa: PLC0415
    from alpaca.trading.client import TradingClient  # noqa: PLC0415

    if kill_switch_active():
        log.warning("Strategy job skipped — kill switch is HALTED")
        return

    try:
        cfg = get_config()
        client = TradingClient(cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY, paper=cfg.PAPER)
        nav = float(client.get_account().equity)
        log.info("Strategy cycle start | NAV=$%.2f", nav)

        # --- Detect bracket exits since last run ---
        # Alpaca positions that we expected to be open but are now gone have
        # been closed by their bracket child orders (stop or TP filled).
        try:
            # Exclude the defence-beta sleeve — it is held outside gated logic and
            # must never enter expected_positions.json (else check_exits would sell it).
            current_positions: set[str] = {
                p.symbol for p in client.get_all_positions()
            } - {cfg.SLEEVE_SYMBOL}
        except Exception as exc:
            log.warning("Could not fetch positions — skipping exit detection: %s", exc)
            current_positions = set()

        expected_positions = load_expected_positions()
        closed_symbols = expected_positions - current_positions
        for sym in closed_symbols:
            record_exit(sym)
            log.info("EXIT detected | %s — cooldown 3 trading days", sym)

        # Update expected positions to match current reality
        save_expected_positions(current_positions)

        # Load exit state once for all cooldown checks this cycle
        exit_state = load_exit_state()

        # --- Gate evaluation ---
        results = evaluate_all_symbols(nav=nav)

        open_gates = [r for r in results if r.all_open]
        log.info(
            "Gate summary | open=%d closed=%d",
            len(open_gates), len(results) - len(open_gates),
        )

        # Per-gate blocker breakdown — makes zero-trade periods attributable
        # (which gate is doing the blocking) without per-symbol log spam.
        closed_results = [r for r in results if not r.all_open]
        if closed_results:
            blocker_tags = {
                "regime": "REGIME=CLOSED",
                "momentum": "MOMENTUM=CLOSED",
                "sentiment": "SENTIMENT=CLOSED",
                "safe_haven": "SH_HOSTILE_REGIME",
                "fxy_52w": "FXY_52W_LOW_GATE",
            }
            counts = {
                name: sum(1 for r in closed_results if tag in r.reason)
                for name, tag in blocker_tags.items()
            }
            breakdown = " ".join(f"{k}={v}" for k, v in counts.items() if v)
            log.info("Gate blockers | %s", breakdown or "unparsed reason")

        if not open_gates:
            log.info("No gates open — no orders submitted")
            return

        for result in open_gates:
            try:
                instr = build_trade_instruction(result, nav)

                # Cooldown check — block re-entry after any bracket exit
                if is_in_cooldown(instr.symbol, exit_state):
                    log.info(
                        "COOLDOWN | %s — skipping entry (3-day post-exit block)",
                        instr.symbol,
                    )
                    continue

                log.info(
                    "TRADE INSTRUCTION | %s %s qty=%d @ $%.2f | alloc=%.2f%% NAV | kelly=%.4f",
                    instr.symbol, instr.direction, instr.quantity,
                    instr.entry_price, instr.allocation_pct * 100,
                    instr.kelly_fraction,
                )
                if submit_entry(instr, cfg):
                    current_positions.add(instr.symbol)
                    save_expected_positions(current_positions)
            except ValueError as exc:
                log.warning("Instruction skipped for %s: %s", result.symbol, exc)

    except Exception as exc:
        log.error("Strategy job failed: %s", exc)


# ---------------------------------------------------------------------------
# Exit monitor
# ---------------------------------------------------------------------------
def job_check_exits() -> None:
    """Re-evaluate momentum gate for all open positions; exit on CLOSED signal."""
    from execution.order_executor import check_exits  # noqa: PLC0415
    from config import get_config  # noqa: PLC0415
    try:
        cfg = get_config()
        check_exits(cfg)
    except Exception as exc:
        log.error("check_exits job failed: %s", exc)


# ---------------------------------------------------------------------------
# Defence-beta sleeve rebalance
# ---------------------------------------------------------------------------
def job_sleeve_rebalance() -> None:
    """
    Maintain the defence-beta sleeve at target weight (quarterly + drift band).
    Runs daily; cadence/band/market-open guards live inside rebalance_sleeve().
    No-op when SLEEVE_ENABLED is false. Independent of the kill switch.
    """
    from execution.sleeve_executor import rebalance_sleeve  # noqa: PLC0415
    from config import get_config  # noqa: PLC0415
    try:
        rebalance_sleeve(get_config())
    except Exception as exc:
        log.error("Sleeve rebalance job failed: %s", exc)


# ---------------------------------------------------------------------------
# Daily summary
# ---------------------------------------------------------------------------
def job_daily_summary() -> None:
    """
    Send one Telegram per day (21:00 UTC) summarising the day's activity.
    The absence of this message is itself an alarm signal — it means the bot
    was down at send time — so each section degrades to 'n/a' rather than
    letting one failure suppress the whole message.
    """
    from config import get_config  # noqa: PLC0415
    from alpaca.trading.client import TradingClient  # noqa: PLC0415

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # --- Activity counts from today's process.log lines ---
    starts = cycles = trades = 0
    last_gate_summary = "n/a"
    try:
        with open(_LOG_DIR / "process.log", encoding="utf-8") as f:
            for line in f:
                if not line.startswith(today):
                    continue
                if "Synk bot starting" in line:
                    starts += 1
                elif "Strategy cycle start" in line:
                    cycles += 1
                elif "TRADE INSTRUCTION" in line:
                    trades += 1
                elif "Gate summary" in line:
                    last_gate_summary = line.split("| INFO | ")[-1].strip()
    except OSError as exc:
        log.error("Daily summary: cannot read process.log: %s", exc)

    # --- Account snapshot (same client pattern as job_strategy) ---
    equity_txt = sleeve_txt = "n/a"
    try:
        cfg = get_config()
        client = TradingClient(cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY, paper=cfg.PAPER)
        equity = float(client.get_account().equity)
        sleeve_value = sum(
            float(p.market_value)
            for p in client.get_all_positions()
            if p.symbol == cfg.SLEEVE_SYMBOL
        )
        equity_txt = f"${equity:,.2f}"
        sleeve_txt = f"${sleeve_value:,.2f} ({sleeve_value / equity:.1%})" if equity else "n/a"
    except Exception as exc:
        log.error("Daily summary: account snapshot failed: %s", exc)

    # --- Kill switch state ---
    ks_state = "UNKNOWN"
    try:
        ks = json.loads((_LOG_DIR / "kill_switch_state.json").read_text(encoding="utf-8"))
        ks_state = ks.get("state", "UNKNOWN")
    except (OSError, json.JSONDecodeError):
        pass

    send_telegram(
        f"\U0001F4CA *Synk daily summary* ({today})\n"
        f"Equity: {equity_txt} | Sleeve: {sleeve_txt}\n"
        f"Kill switch: {ks_state}\n"
        f"Strategy cycles: {cycles} | Trade instructions: {trades} | Restarts: {starts}\n"
        f"Last gates: {last_gate_summary}"
    )
    log.info(
        "Daily summary sent | cycles=%d trades=%d restarts=%d ks=%s",
        cycles, trades, starts, ks_state,
    )


# ---------------------------------------------------------------------------
# FinBERT drift monitor
# ---------------------------------------------------------------------------
def job_finbert_drift() -> None:
    """Check FinBERT output distribution for drift; log result to health.jsonl."""
    from signals.finbert_drift_monitor import run_batch_check  # noqa: PLC0415
    try:
        result = run_batch_check(window_days=7)
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(_LOG_DIR / "health.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(result) + "\n")
        log.info("FinBERT drift: %s (n=%d)", result["status"], result["n"])
    except Exception as exc:
        log.error("FinBERT drift job failed: %s", exc)


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_scheduler: "BlockingScheduler | None" = None


def _handle_shutdown(signum: int, frame: object) -> None:
    log.info("Shutdown signal %d received — stopping scheduler", signum)
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    global _scheduler

    load_dotenv()

    log.info("=" * 60)
    log.info("Synk bot starting | PID=%d", os.getpid())
    log.info("=" * 60)

    # Write heartbeat immediately so watchdog sees us from the first cycle
    job_heartbeat()

    # Startup recovery: misfire_grace_time only fires catch-up jobs if the
    # scheduler was already running when the scheduled time passed. A cold
    # restart after downtime must refresh stale data proactively.
    log.info("Startup data check — refreshing stale feeds if needed")
    job_gpr_refresh()  # no-op if file < 24h old; re-downloads if stale
    job_sentiment()    # always run: cache expires in 90 min

    scheduler = BlockingScheduler(timezone="UTC")
    _scheduler = scheduler

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    # Heartbeat — every 5 min, starting immediately
    scheduler.add_job(
        job_heartbeat,
        IntervalTrigger(minutes=5),
        id="heartbeat",
        max_instances=1,
        coalesce=True,
    )

    # Kill switch — every 15 min, starting immediately
    scheduler.add_job(
        job_kill_switch_check,
        IntervalTrigger(minutes=15),
        id="kill_switch",
        max_instances=1,
        coalesce=True,
    )

    # Sentiment — hourly at :00
    scheduler.add_job(
        job_sentiment,
        CronTrigger(minute=0),
        id="sentiment",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    # Prices — hourly at :00
    scheduler.add_job(
        job_prices,
        CronTrigger(minute=0),
        id="prices",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    # Health check — hourly at :05 (after data jobs start)
    scheduler.add_job(
        job_health_check,
        CronTrigger(minute=5),
        id="health_check",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    # Strategy — hourly at :10 (10 min after data refresh, enough headroom)
    scheduler.add_job(
        job_strategy,
        CronTrigger(minute=10),
        id="strategy",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    # Exit monitor — hourly at :10, alongside strategy (evaluates open positions)
    scheduler.add_job(
        job_check_exits,
        CronTrigger(minute=10),
        id="check_exits",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    # FinBERT drift — daily at 08:00 UTC (daily cadence sufficient)
    scheduler.add_job(
        job_finbert_drift,
        CronTrigger(hour=8, minute=0),
        id="finbert_drift",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # GPR daily refresh — 16:30 ET after market close
    scheduler.add_job(
        job_gpr_refresh,
        CronTrigger(hour=16, minute=30, timezone="America/New_York"),
        id="gpr_refresh",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Defence-beta sleeve — daily check at 15:00 UTC (mid US morning); the
    # function self-gates on quarterly cadence + drift band + market-open, so a
    # daily trigger is robust to weekends/holidays. No-op if SLEEVE_ENABLED=false.
    scheduler.add_job(
        job_sleeve_rebalance,
        CronTrigger(hour=15, minute=0),
        id="sleeve_rebalance",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Daily summary — 21:00 UTC (22:00 UK summer); silence = bot is down
    scheduler.add_job(
        job_daily_summary,
        CronTrigger(hour=21, minute=0),
        id="daily_summary",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    job_ids = [j.id for j in scheduler.get_jobs()]
    log.info("Scheduler armed | jobs: %s", job_ids)
    log.info(
        "Schedule: heartbeat=5min | kill_switch=15min | "
        "sentiment/prices=:00 | health=:05 | strategy/exits=:10 | "
        "finbert_drift=08:00 UTC | gpr=16:30 ET | sleeve=15:00 UTC | "
        "daily_summary=21:00 UTC"
    )

    send_telegram(
        f"🟢 *Synk bot started*\n"
        f"PID: {os.getpid()}\n"
        f"Jobs armed: {len(job_ids)}\n"
        f"Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )

    try:
        scheduler.start()  # blocks until shutdown signal
    except (KeyboardInterrupt, SystemExit):
        log.info("Synk bot stopped")


if __name__ == "__main__":
    main()

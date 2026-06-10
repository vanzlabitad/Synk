"""
watchdog

Independent monitoring process for the Synk bot. Runs as a standalone script
scheduled via Windows Task Scheduler every 15 minutes — NOT as a thread inside
main.py. If main.py hangs or crashes, this process still runs.

Checks on each execution:
    1. Heartbeat freshness: reads logs/heartbeat.json.
       If last_alive > 10 min ago → restart the bot (Windows: end + re-run the
       SynkBot scheduled task; other OSes: left to systemd Restart=always),
       then Telegram alert "HEARTBEAT LOST" at most once per 60 min.
    2. Kill switch state: reads logs/kill_switch_state.json.
       If state=HALTED and last re-alert was > 60 min ago → re-alert "SYNK STILL HALTED".

Alert deduplication: tracks last alert times in logs/watchdog_state.json so
repeated 15-min runs don't spam Telegram.

Own log: logs/watchdog.log (separate from process.log to survive main.py crashes).

Usage:
    python alerts/watchdog.py

Schedule via Task Scheduler:
    Program:   python
    Arguments: C:\\path\\to\\synk\\alerts\\watchdog.py
    Start in:  C:\\path\\to\\synk
    Trigger:   Every 15 minutes
"""

# DEPLOYMENT: open second terminal, cd to project root, run:
#   python alerts/watchdog.py
# Or schedule via Windows Task Scheduler (every 15 min) — see docstring above.
# Keep running alongside main.py at all times.

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent.parent  # synk/ root
_LOG_DIR = _HERE / "logs"
_HEARTBEAT_PATH = _LOG_DIR / "heartbeat.json"
_KS_STATE_PATH = _LOG_DIR / "kill_switch_state.json"
_WD_STATE_PATH = _LOG_DIR / "watchdog_state.json"

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from dotenv import load_dotenv  # noqa: E402

from alerts.telegram_util import send_telegram  # noqa: E402

load_dotenv()

# ---------------------------------------------------------------------------
# Tuneable thresholds
# ---------------------------------------------------------------------------
_HEARTBEAT_MAX_AGE_SECONDS = 10 * 60   # alert if heartbeat > 10 min old
_HEARTBEAT_REALERT_INTERVAL = 60 * 60  # re-alert on stale heartbeat every 60 min
_REHALT_ALERT_INTERVAL = 60 * 60       # re-alert on HALTED state every 60 min
_BOT_TASK_NAME = "SynkBot"             # Windows Task Scheduler task to restart

# ---------------------------------------------------------------------------
# Logging — watchdog.log is separate so it survives main.py crashes
# ---------------------------------------------------------------------------
def _setup_logging() -> logging.Logger:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("watchdog")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s UTC | %(levelname)s | %(message)s")
    fmt.converter = time.gmtime

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    fh = logging.FileHandler(_LOG_DIR / "watchdog.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


log = _setup_logging()


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------
def _load_wd_state() -> dict:
    if not _WD_STATE_PATH.exists():
        return {"last_heartbeat_alert_utc": None, "last_halted_alert_utc": None}
    try:
        return json.loads(_WD_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"last_heartbeat_alert_utc": None, "last_halted_alert_utc": None}


def _save_wd_state(state: dict) -> None:
    tmp = _WD_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, _WD_STATE_PATH)


def _seconds_since(iso_timestamp: str | None) -> float:
    """Return seconds since an ISO 8601 UTC timestamp, or infinity if None."""
    if iso_timestamp is None:
        return float("inf")
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


# ---------------------------------------------------------------------------
# Bot restart
# ---------------------------------------------------------------------------
def _attempt_bot_restart() -> str:
    """
    Try to restart the bot via the OS scheduler. Returns a short status string
    for the Telegram alert.

    Windows: end the SynkBot task first (kills a hung python — a stale heartbeat
    with a still-"Running" task means a hang, and /run alone would be ignored
    under MultipleInstancesPolicy=IgnoreNew), then start it fresh.
    Other OSes: systemd owns restarts (Restart=always) — never intervene.
    """
    if platform.system() != "Windows":
        return "restart left to systemd"

    try:
        # /end fails harmlessly if the task is not running — ignore its rc.
        subprocess.run(
            ["schtasks", "/end", "/tn", _BOT_TASK_NAME],
            capture_output=True, text=True, timeout=30,
        )
        result = subprocess.run(
            ["schtasks", "/run", "/tn", _BOT_TASK_NAME],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("Bot restart failed: %s", exc)
        return f"restart FAILED ({exc})"

    if result.returncode == 0:
        log.info("Bot restart attempted: schtasks /run %s succeeded", _BOT_TASK_NAME)
        return f"restart attempted via {_BOT_TASK_NAME} task"

    log.error(
        "Bot restart failed: schtasks exit %d | %s",
        result.returncode, (result.stderr or result.stdout).strip(),
    )
    return f"restart FAILED (schtasks exit {result.returncode})"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
def _check_heartbeat(wd_state: dict) -> None:
    """Restart the bot and alert if heartbeat is older than _HEARTBEAT_MAX_AGE_SECONDS."""
    if not _HEARTBEAT_PATH.exists():
        log.warning("Heartbeat file missing — main.py may not have started yet")
        return

    try:
        hb = json.loads(_HEARTBEAT_PATH.read_text(encoding="utf-8"))
        last_alive = hb.get("last_alive")
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Cannot read heartbeat: %s", exc)
        return

    age = _seconds_since(last_alive)
    log.info("Heartbeat age: %.1f min", age / 60)

    if age <= _HEARTBEAT_MAX_AGE_SECONDS:
        return

    # Stale heartbeat — restart first (every cycle; it's idempotent), then alert
    # at most once per _HEARTBEAT_REALERT_INTERVAL so Telegram isn't spammed.
    restart_status = _attempt_bot_restart()

    msg = (
        f"HEARTBEAT LOST — last alive {age/60:.0f} min ago "
        f"(threshold: {_HEARTBEAT_MAX_AGE_SECONDS//60} min). "
        f"{restart_status}."
    )
    log.critical(msg)

    since_last_alert = _seconds_since(wd_state.get("last_heartbeat_alert_utc"))
    if since_last_alert < _HEARTBEAT_REALERT_INTERVAL:
        log.info(
            "Re-alert suppressed — last sent %.0f min ago (interval: %d min)",
            since_last_alert / 60, _HEARTBEAT_REALERT_INTERVAL // 60,
        )
        return

    send_telegram(msg)
    wd_state["last_heartbeat_alert_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_kill_switch(wd_state: dict) -> None:
    """Re-alert if kill switch is HALTED and last alert was > 60 min ago."""
    if not _KS_STATE_PATH.exists():
        return

    try:
        ks = json.loads(_KS_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Cannot read kill switch state: %s", exc)
        return

    if ks.get("state") != "HALTED":
        log.info("Kill switch: ACTIVE")
        return

    halt_reason = ks.get("halt_reason", "unknown")
    halted_at = ks.get("halted_at", "unknown")
    log.warning("Kill switch is HALTED: %s (since %s)", halt_reason, halted_at)

    since_last_alert = _seconds_since(wd_state.get("last_halted_alert_utc"))
    if since_last_alert < _REHALT_ALERT_INTERVAL:
        log.info(
            "Re-alert suppressed — last sent %.0f min ago (interval: %d min)",
            since_last_alert / 60, _REHALT_ALERT_INTERVAL // 60,
        )
        return

    msg = f"STILL HALTED — reason: {halt_reason} | halted since: {halted_at}. Manual reset required."
    send_telegram(msg)
    wd_state["last_halted_alert_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Entry point — one watchdog cycle
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("Watchdog cycle starting")
    wd_state = _load_wd_state()

    _check_heartbeat(wd_state)
    _check_kill_switch(wd_state)

    _save_wd_state(wd_state)
    log.info("Watchdog cycle complete")

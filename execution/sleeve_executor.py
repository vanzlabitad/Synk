"""
execution/sleeve_executor

Defence-beta SLEEVE execution — a separate buy-and-hold defence-ETF allocation
(default PPA) held alongside, but OUTSIDE, the gated strategy.

Rationale: the frequency/alpha investigation (backtest/FINDINGS_frequency_alpha_*.md)
showed the defence contribution is BETA, not timing alpha. The chosen design is
to take that beta deliberately as a passive sleeve (near-zero correlation with the
gated strategy -> combined Sharpe 0.43 -> 0.82 at ~15% weight), NOT via gated
single-names.

Isolation (user decision — full carve-out):
    - This module has its OWN execution path. It does NOT consult the kill switch
      (kill_switch_active()) — the sleeve is independent of gated risk halts.
    - It NEVER writes trades.jsonl or expected_positions.json, so the gated
      strategy's exit/momentum logic never touches it.
    - kill_switch.py separately excludes the sleeve's market value from its
      drawdown/daily-loss basis (gated equity = total - sleeve value).

Rebalance policy: quarterly + drift band. The scheduled job runs daily but this
module only acts when (a) >= SLEEVE_REBALANCE_DAYS since the last rebalance
(or never rebalanced — initial seed) AND (b) the drift from target exceeds
SLEEVE_DRIFT_BAND AND (c) the market is open.

Sizing: target_value = SLEEVE_TARGET_WEIGHT * total_account_equity.
    underweight beyond band -> BUY (notional, supports fractional)
    overweight  beyond band -> SELL (whole-share qty)

SAFETY: ships disabled (SLEEVE_ENABLED=false) and bound to cfg.PAPER. Enabling
live and funding the account is a manual operator action. This module only ever
acts on the operator's own automated system.

Usage (from synk/ root, paper):
    SLEEVE_ENABLED=true python execution/sleeve_executor.py

Deps: config.py, alerts/telegram_util.py, alpaca-py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths and sys.path
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent.parent  # synk/ root

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from alerts.telegram_util import send_telegram  # noqa: E402

_LOG_DIR = _HERE / "logs"
_SLEEVE_STATE = _LOG_DIR / "sleeve_state.json"
_SLEEVE_LOG = _LOG_DIR / "sleeve_log.jsonl"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_MIN_TRADE_NOTIONAL = 50.0  # skip dust rebalances below this $ delta

# ---------------------------------------------------------------------------
# Logging — stdout + logs/process.log, UTC timestamps
# ---------------------------------------------------------------------------
def _setup_logging() -> logging.Logger:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("sleeve_executor")
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
# Pure decision logic (no IO — unit-tested in tests/sleeve_test.py)
# ---------------------------------------------------------------------------
def compute_rebalance(
    total_equity: float,
    sleeve_value: float,
    price: float,
    target_weight: float,
    drift_band: float,
    min_notional: float = _MIN_TRADE_NOTIONAL,
) -> dict:
    """
    Decide the rebalance action for the sleeve. Pure — no side effects.

    Args:
        total_equity:  total account equity ($).
        sleeve_value:  current market value of the sleeve position ($; 0 if none).
        price:         current sleeve price ($/share); used to size SELL qty.
        target_weight: desired sleeve weight (fraction of total_equity).
        drift_band:    act only if abs(current_weight - target_weight) > this.
        min_notional:  skip trades whose $ delta is below this (dust guard).

    Returns a dict:
        {action: 'BUY'|'SELL'|'HOLD', notional: float, qty: int,
         current_weight: float, target_weight: float, drift: float,
         target_value: float, delta_value: float, reason: str}
    """
    out = {
        "action": "HOLD",
        "notional": 0.0,
        "qty": 0,
        "current_weight": 0.0,
        "target_weight": target_weight,
        "drift": 0.0,
        "target_value": 0.0,
        "delta_value": 0.0,
        "reason": "",
    }

    if total_equity <= 0:
        out["reason"] = "non-positive equity"
        return out

    current_weight = sleeve_value / total_equity
    target_value = target_weight * total_equity
    delta_value = target_value - sleeve_value  # +ve = underweight (buy)
    drift = current_weight - target_weight

    out["current_weight"] = round(current_weight, 4)
    out["drift"] = round(drift, 4)
    out["target_value"] = round(target_value, 2)
    out["delta_value"] = round(delta_value, 2)

    if abs(drift) <= drift_band:
        out["reason"] = f"within band (|drift|={abs(drift):.4f} <= {drift_band})"
        return out

    if abs(delta_value) < min_notional:
        out["reason"] = f"delta ${abs(delta_value):.2f} < min ${min_notional:.0f} (dust)"
        return out

    if delta_value > 0:
        out["action"] = "BUY"
        out["notional"] = round(delta_value, 2)
        out["reason"] = f"underweight: w={current_weight:.4f} < target {target_weight:.4f}"
    else:
        # Sell whole shares worth |delta_value| (avoids fractional-sell edge cases)
        qty = int(abs(delta_value) / price) if price > 0 else 0
        if qty <= 0:
            out["action"] = "HOLD"
            out["reason"] = "overweight but < 1 share to sell"
            return out
        out["action"] = "SELL"
        out["qty"] = qty
        out["reason"] = f"overweight: w={current_weight:.4f} > target {target_weight:.4f}"

    return out


# ---------------------------------------------------------------------------
# State (atomic write — mirrors kill_switch._save_state)
# ---------------------------------------------------------------------------
def _load_state() -> dict:
    if not _SLEEVE_STATE.exists():
        return {}
    try:
        return json.loads(_SLEEVE_STATE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _SLEEVE_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    for delay in (0.2, 0.5, 1.0, 2.0):
        try:
            os.replace(tmp, _SLEEVE_STATE)
            return
        except OSError:
            time.sleep(delay)
    os.replace(tmp, _SLEEVE_STATE)  # final attempt; raises if still locked


def _append_log(record: dict) -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_SLEEVE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _days_since(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        then = datetime.fromisoformat(iso_ts)
        return (datetime.now(timezone.utc) - then).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def rebalance_sleeve(cfg: object) -> None:
    """
    Maintain the defence sleeve at its target weight. Quarterly + drift band.
    Independent of the kill switch. Paper-bound via cfg.PAPER. Never raises.
    """
    from alpaca.trading.client import TradingClient  # noqa: PLC0415
    from alpaca.trading.requests import MarketOrderRequest  # noqa: PLC0415
    from alpaca.trading.enums import OrderSide, TimeInForce  # noqa: PLC0415

    symbol = getattr(cfg, "SLEEVE_SYMBOL", "PPA")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not getattr(cfg, "SLEEVE_ENABLED", False):
        log.info("Sleeve disabled (SLEEVE_ENABLED=false) — no-op")
        return

    try:
        client = TradingClient(  # type: ignore[attr-defined]
            cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY, paper=cfg.PAPER  # type: ignore[attr-defined]
        )
    except Exception as exc:
        log.error("Sleeve: client init failed: %s", exc)
        send_telegram(f"\U0001f534 *SYNK SLEEVE FAILED* | {symbol} | client error")
        return

    try:
        # Market-open guard — avoid rejected/queued orders when closed
        try:
            if not client.get_clock().is_open:
                log.info("Sleeve: market closed — skipping (will retry next run)")
                return
        except Exception as exc:
            log.warning("Sleeve: could not read market clock (%s) — skipping", exc)
            return

        # Cadence gate (quarterly): skip if rebalanced within SLEEVE_REBALANCE_DAYS
        state = _load_state()
        elapsed = _days_since(state.get("last_rebalance_utc"))
        min_days = getattr(cfg, "SLEEVE_REBALANCE_DAYS", 90)
        if elapsed is not None and elapsed < min_days:
            log.info(
                "Sleeve: %.1f days since last rebalance (< %d) — skipping",
                elapsed, min_days,
            )
            return

        # Account equity + current sleeve position
        acct = client.get_account()
        total_equity = float(acct.equity)

        sleeve_value = 0.0
        price = 0.0
        held_qty = 0.0
        try:
            for p in client.get_all_positions():
                if p.symbol == symbol:
                    sleeve_value = float(p.market_value)
                    price = float(p.current_price)
                    held_qty = float(p.qty)
                    break
        except Exception as exc:
            log.error("Sleeve: could not fetch positions: %s", exc)
            return

        # If we hold nothing yet, we need a price for SELL sizing only; BUY uses
        # notional and does not need price. Fetch a quote if price is unknown.
        if price <= 0:
            try:
                from alpaca.data.historical import StockHistoricalDataClient  # noqa: PLC0415
                from alpaca.data.requests import StockLatestTradeRequest  # noqa: PLC0415
                data_client = StockHistoricalDataClient(cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY)
                trade = data_client.get_stock_latest_trade(
                    StockLatestTradeRequest(symbol_or_symbols=symbol)
                )
                price = float(trade[symbol].price)
            except Exception as exc:
                log.warning("Sleeve: could not fetch %s price (%s) — BUY can still proceed", symbol, exc)

        decision = compute_rebalance(
            total_equity=total_equity,
            sleeve_value=sleeve_value,
            price=price,
            target_weight=getattr(cfg, "SLEEVE_TARGET_WEIGHT", 0.15),
            drift_band=getattr(cfg, "SLEEVE_DRIFT_BAND", 0.03),
        )

        log.info(
            "Sleeve check | %s | equity=$%.2f sleeve=$%.2f w=%.4f target=%.4f | action=%s (%s)",
            symbol, total_equity, sleeve_value, decision["current_weight"],
            decision["target_weight"], decision["action"], decision["reason"],
        )

        if decision["action"] == "HOLD":
            # Record the check but do not advance last_rebalance (no trade made)
            _append_log({"timestamp": now, "symbol": symbol, **decision, "submitted": False})
            return

        # --- Submit the rebalance order ---
        if decision["action"] == "BUY":
            order_req = MarketOrderRequest(
                symbol=symbol,
                notional=decision["notional"],
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
        else:  # SELL
            order_req = MarketOrderRequest(
                symbol=symbol,
                qty=decision["qty"],
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )

        order = client.submit_order(order_req)

        log.info(
            "SLEEVE %s SUBMITTED | %s | notional=$%.2f qty=%d | id=%s",
            decision["action"], symbol, decision["notional"], decision["qty"], order.id,
        )

        _append_log({
            "timestamp": now, "symbol": symbol, **decision,
            "submitted": True, "order_id": str(order.id),
            "total_equity": round(total_equity, 2),
        })
        _save_state({
            "last_rebalance_utc": now,
            "symbol": symbol,
            "target_weight": decision["target_weight"],
            "last_action": decision["action"],
            "last_weight_before": decision["current_weight"],
        })

        send_telegram(
            f"\U0001f535 *SYNK SLEEVE {decision['action']}*\n"
            f"{symbol} | "
            + (f"BUY ${decision['notional']:.0f}\n" if decision["action"] == "BUY"
               else f"SELL {decision['qty']} sh\n")
            + f"Weight: {decision['current_weight']*100:.1f}% -> target {decision['target_weight']*100:.0f}%\n"
            f"{'PAPER' if cfg.PAPER else 'LIVE'}"
        )

    except Exception as exc:
        log.error("Sleeve rebalance failed: %s", exc)
        send_telegram(f"\U0001f534 *SYNK SLEEVE FAILED* | {symbol} | {exc}")


# ---------------------------------------------------------------------------
# Entry point — one manual rebalance cycle (paper seeding / testing)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from config import get_config  # noqa: PLC0415

    cfg = get_config()
    print("\n--- Sleeve Rebalance (manual run) ---")
    print(f"Symbol:        {cfg.SLEEVE_SYMBOL}")
    print(f"Enabled:       {cfg.SLEEVE_ENABLED}")
    print(f"Target weight: {cfg.SLEEVE_TARGET_WEIGHT:.0%}")
    print(f"Mode:          {'PAPER' if cfg.PAPER else 'LIVE'}")
    rebalance_sleeve(cfg)
    print("Done. See logs/sleeve_log.jsonl and logs/sleeve_state.json.")

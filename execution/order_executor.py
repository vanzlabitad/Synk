"""
execution/order_executor

Order submission layer for Synk. Two execution paths:

submit_bracket_order(instruction) — legacy bracket entry (stop + take-profit
    legs). Kept for reference; main.py now uses submit_entry instead.

submit_entry(instruction, cfg) -> bool
    Plain market BUY. Logs to logs/trades.jsonl. Sends Telegram entry alert.

submit_exit(symbol, quantity, cfg) -> bool
    Plain market SELL. Updates trades.jsonl with exit data. Calls record_exit().
    Sends Telegram exit alert.

check_exits(cfg) -> None
    Re-evaluates momentum gate for every expected open position. Calls
    submit_exit() for any position where momentum has flipped CLOSED.

Skips submission if kill switch is HALTED or position already exists.
Three-tier logging: stdout + logs/process.log + logs/orders.jsonl.

Deps: config.py, risk/kill_switch.py, strategy/synk_strategy.py, alpaca-py
Run from synk/ root.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import re

# ---------------------------------------------------------------------------
# Paths and sys.path
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent.parent  # synk/ root

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from alerts.telegram_util import send_telegram  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_STOP_LOSS_POSITION_PCT = 0.08  # 8% position-level stop (entry_price × 0.92); ~0.32% portfolio loss per stop-out at 4% position size
TAKE_PROFIT_PCT = 0.03          # legacy bracket path only — not used in main submit_entry flow
_LOG_DIR = _HERE / "logs"
_ORDERS_JSONL = _LOG_DIR / "orders.jsonl"
_TRADES_JSONL = _LOG_DIR / "trades.jsonl"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _setup_logging() -> logging.Logger:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("order_executor")
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
# Domain types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OrderResult:
    timestamp: str
    symbol: str
    submitted: bool
    order_id: str            # Alpaca order UUID if submitted, "" otherwise
    order_status: str        # Alpaca status string or an error label
    quantity: int
    entry_price: float       # last close at signal time — not actual fill price
    stop_price: float
    take_profit_price: float
    error: str               # "" if no error


# ---------------------------------------------------------------------------
# trades.jsonl helpers
# ---------------------------------------------------------------------------
def _append_trade(record: dict) -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_TRADES_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _find_open_trade_entry_price(symbol: str) -> float | None:
    """Return entry_price of the most recent open (no exit_timestamp) trade for symbol."""
    if not _TRADES_JSONL.exists():
        return None
    try:
        entry_price = None
        with open(_TRADES_JSONL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                trade = json.loads(line)
                if trade.get("symbol") == symbol and not trade.get("exit_timestamp"):
                    entry_price = trade.get("entry_price")
        return float(entry_price) if entry_price is not None else None
    except Exception:
        return None


def _find_open_trade_entry_timestamp(symbol: str) -> str | None:
    """Return entry timestamp of the most recent open (no exit_timestamp) trade for symbol."""
    if not _TRADES_JSONL.exists():
        return None
    try:
        entry_ts = None
        with open(_TRADES_JSONL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                trade = json.loads(line)
                if trade.get("symbol") == symbol and not trade.get("exit_timestamp"):
                    entry_ts = trade.get("timestamp")
        return entry_ts
    except Exception:
        return None


def _update_trade_exit(
    symbol: str, exit_price: float, exit_ts: str, pnl_pct: float, exit_reason: str = ""
) -> None:
    """Update the most recent open trade for symbol with exit data. Atomic write."""
    if not _TRADES_JSONL.exists():
        return
    try:
        raw = _TRADES_JSONL.read_text(encoding="utf-8")
        lines = raw.splitlines()

        # Find index of last open trade for symbol
        last_open_idx: int | None = None
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                trade = json.loads(line)
                if trade.get("symbol") == symbol and not trade.get("exit_timestamp"):
                    last_open_idx = i
            except json.JSONDecodeError:
                pass

        if last_open_idx is None:
            log.warning("_update_trade_exit: no open trade found for %s", symbol)
            return

        trade = json.loads(lines[last_open_idx])
        trade["exit_price"] = exit_price
        trade["exit_timestamp"] = exit_ts
        trade["pnl_pct"] = round(pnl_pct, 4)
        trade["exit_reason"] = exit_reason
        lines[last_open_idx] = json.dumps(trade, ensure_ascii=False)

        tmp = _TRADES_JSONL.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, _TRADES_JSONL)
    except Exception as exc:
        log.error("Could not update trades.jsonl for %s: %s", symbol, exc)


# ---------------------------------------------------------------------------
# Position deduplication helper
# ---------------------------------------------------------------------------
def _has_open_position(client: object, symbol: str) -> bool:
    """
    Return True if Alpaca already holds an open position in symbol.
    On API error, returns True (conservative — skip rather than double-enter).
    """
    try:
        positions = client.get_all_positions()  # type: ignore[attr-defined]
        return any(p.symbol == symbol for p in positions)
    except Exception as exc:
        log.warning(
            "Could not fetch open positions for %s — skipping order (conservative): %s",
            symbol, exc,
        )
        return True  # fail closed


# ---------------------------------------------------------------------------
# Legacy bracket order (kept for reference — main.py uses submit_entry now)
# ---------------------------------------------------------------------------
def submit_bracket_order(instruction: "TradeInstruction") -> OrderResult:  # type: ignore[name-defined]
    """
    Submit a bracket market order to Alpaca for the given TradeInstruction.

    Stop-loss:   _STOP_LOSS_POSITION_PCT (8%) below entry_price.
    Take-profit: TAKE_PROFIT_PCT (3%) above entry_price.

    Returns OrderResult in all cases — never raises.
    """
    from risk.kill_switch import kill_switch_active  # noqa: PLC0415
    from config import get_config  # noqa: PLC0415
    from alpaca.trading.client import TradingClient  # noqa: PLC0415
    from alpaca.trading.requests import (  # noqa: PLC0415
        MarketOrderRequest,
        TakeProfitRequest,
        StopLossRequest,
    )
    from alpaca.trading.enums import OrderSide, OrderClass, TimeInForce  # noqa: PLC0415

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stop_price = round(instruction.entry_price * (1 - _STOP_LOSS_POSITION_PCT), 2)
    take_profit_price = round(instruction.entry_price * (1 + TAKE_PROFIT_PCT), 2)

    def _fail(label: str, msg: str) -> OrderResult:
        result = OrderResult(
            timestamp=now,
            symbol=instruction.symbol,
            submitted=False,
            order_id="",
            order_status=label,
            quantity=instruction.quantity,
            entry_price=instruction.entry_price,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            error=msg,
        )
        _append_orders_jsonl(result)
        return result

    if kill_switch_active():
        log.warning("ORDER SUPPRESSED | %s — kill switch HALTED", instruction.symbol)
        return _fail("KILL_SWITCH_HALTED", "Kill switch is HALTED — order suppressed")

    try:
        cfg = get_config()
        client = TradingClient(cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY, paper=cfg.PAPER)
    except Exception as exc:
        log.error("ORDER FAILED | %s — could not init Alpaca client: %s", instruction.symbol, exc)
        return _fail("CLIENT_INIT_ERROR", str(exc))

    if _has_open_position(client, instruction.symbol):
        log.info("ORDER SKIPPED | %s — open position already exists", instruction.symbol)
        return _fail("POSITION_EXISTS", "Open position already exists — bracket order skipped")

    try:
        order_data = MarketOrderRequest(
            symbol=instruction.symbol,
            qty=instruction.quantity,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=take_profit_price),
            stop_loss=StopLossRequest(stop_price=stop_price),
        )

        log.info(
            "Submitting bracket order | %s BUY qty=%d | "
            "stop=$%.2f (-%d%%) | take_profit=$%.2f (+%d%%) | entry_ref=$%.2f",
            instruction.symbol, instruction.quantity,
            stop_price, int(_STOP_LOSS_POSITION_PCT * 100),
            take_profit_price, int(TAKE_PROFIT_PCT * 100),
            instruction.entry_price,
        )

        order = client.submit_order(order_data)

        result = OrderResult(
            timestamp=now,
            symbol=instruction.symbol,
            submitted=True,
            order_id=str(order.id),
            order_status=str(order.status),
            quantity=instruction.quantity,
            entry_price=instruction.entry_price,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            error="",
        )
        log.info(
            "ORDER SUBMITTED | %s | id=%s status=%s | stop=$%.2f take=$%.2f",
            instruction.symbol, order.id, order.status,
            stop_price, take_profit_price,
        )

    except Exception as exc:
        log.error("ORDER FAILED | %s | %s", instruction.symbol, exc)
        result = OrderResult(
            timestamp=now,
            symbol=instruction.symbol,
            submitted=False,
            order_id="",
            order_status="SUBMISSION_ERROR",
            quantity=instruction.quantity,
            entry_price=instruction.entry_price,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            error=str(exc),
        )

    _append_orders_jsonl(result)
    return result


def _append_orders_jsonl(result: OrderResult) -> None:
    """Append an OrderResult to logs/orders.jsonl."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_ORDERS_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Entry — plain market buy
# ---------------------------------------------------------------------------
def submit_entry(instruction: "TradeInstruction", cfg: object) -> bool:  # type: ignore[name-defined]
    """
    Submit a plain market BUY for the given TradeInstruction.

    On success: appends to logs/trades.jsonl and sends a Telegram entry alert.
    On failure: logs the error, sends a Telegram failure alert, returns False.
    Never raises.
    """
    from risk.kill_switch import kill_switch_active  # noqa: PLC0415
    from alpaca.trading.client import TradingClient  # noqa: PLC0415
    from alpaca.trading.requests import MarketOrderRequest  # noqa: PLC0415
    from alpaca.trading.enums import OrderSide, TimeInForce  # noqa: PLC0415

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if kill_switch_active():
        log.warning("ENTRY SUPPRESSED | %s — kill switch HALTED", instruction.symbol)
        return False

    try:
        client = TradingClient(  # type: ignore[attr-defined]
            cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY, paper=cfg.PAPER  # type: ignore[attr-defined]
        )
    except Exception as exc:
        log.error("ENTRY FAILED | %s — client init: %s", instruction.symbol, exc)
        send_telegram(f"🔴 *SYNK ENTRY FAILED* | {instruction.symbol} | client error")
        return False

    if _has_open_position(client, instruction.symbol):
        log.info("ENTRY SKIPPED | %s — open position already exists", instruction.symbol)
        return False

    try:
        order_data = MarketOrderRequest(
            symbol=instruction.symbol,
            qty=instruction.quantity,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )
        order = client.submit_order(order_data)

        log.info(
            "ENTRY SUBMITTED | %s BUY qty=%d @ ~$%.2f | alloc=%.1f%% NAV | id=%s",
            instruction.symbol, instruction.quantity, instruction.entry_price,
            instruction.allocation_pct * 100, order.id,
        )

        _append_trade({
            "timestamp": now,
            "symbol": instruction.symbol,
            "side": "BUY",
            "quantity": instruction.quantity,
            "entry_price": instruction.entry_price,
            "allocation_pct": instruction.allocation_pct,
            "kelly_fraction": instruction.kelly_fraction,
            "rationale": instruction.rationale,
            "exit_price": None,
            "exit_timestamp": None,
            "pnl_pct": None,
        })

        _regime_m = re.search(r'regime=(\S+)\s+z=([^\s|]+)', instruction.rationale)
        _regime_state = _regime_m.group(1) if _regime_m else "UNKNOWN"
        _z_score = float(_regime_m.group(2)) if _regime_m else 0.0
        send_telegram(
            f"\U0001f7e2 *SYNK ENTRY*\n"
            f"{instruction.symbol} | BUY {instruction.quantity} @ ${instruction.entry_price:.2f}\n"
            f"Alloc: {instruction.allocation_pct * 100:.1f}% NAV\n"
            f"Kelly: {instruction.kelly_fraction:.4f}\n"
            f"Regime: {_regime_state} z={_z_score:+.3f}"
        )
        return True

    except Exception as exc:
        log.error("ENTRY FAILED | %s | %s", instruction.symbol, exc)
        send_telegram(f"\U0001f534 *SYNK ENTRY FAILED* | {instruction.symbol} | {exc}")
        return False


# ---------------------------------------------------------------------------
# Exit — plain market sell
# ---------------------------------------------------------------------------
def submit_exit(symbol: str, quantity: int, cfg: object, exit_reason: str = "") -> bool:
    """
    Submit a plain market SELL for symbol/quantity.

    On success: updates logs/trades.jsonl with exit data, calls record_exit(),
    and sends a Telegram exit alert.
    On failure: logs the error, sends a Telegram failure alert, returns False.
    Never raises.
    """
    from strategy.synk_strategy import record_exit  # noqa: PLC0415
    from alpaca.trading.client import TradingClient  # noqa: PLC0415
    from alpaca.trading.requests import MarketOrderRequest  # noqa: PLC0415
    from alpaca.trading.enums import OrderSide, TimeInForce  # noqa: PLC0415

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    try:
        client = TradingClient(  # type: ignore[attr-defined]
            cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY, paper=cfg.PAPER  # type: ignore[attr-defined]
        )

        # Get current price from Alpaca position before selling (best-effort)
        exit_price = 0.0
        try:
            positions = client.get_all_positions()
            pos = next((p for p in positions if p.symbol == symbol), None)
            if pos:
                exit_price = float(pos.current_price)
        except Exception as exc:
            log.warning("Could not fetch current price for %s: %s", symbol, exc)

        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=quantity,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = client.submit_order(order_data)

        # P&L vs recorded entry price
        entry_price = _find_open_trade_entry_price(symbol)
        pnl_pct = 0.0
        if entry_price and entry_price > 0 and exit_price > 0:
            pnl_pct = (exit_price - entry_price) / entry_price * 100

        # Hold duration
        entry_ts_str = _find_open_trade_entry_timestamp(symbol)
        hold_days = 0
        if entry_ts_str:
            try:
                entry_dt = datetime.fromisoformat(entry_ts_str)
                hold_days = (datetime.now(timezone.utc) - entry_dt).days
            except Exception:
                pass

        log.info(
            "EXIT SUBMITTED | %s SELL qty=%d @ ~$%.2f | PnL: %+.2f%% | id=%s",
            symbol, quantity, exit_price, pnl_pct, order.id,
        )

        _update_trade_exit(symbol, exit_price, now, pnl_pct, exit_reason)
        record_exit(symbol)

        send_telegram(
            f"\U0001f534 *SYNK EXIT*\n"
            f"{symbol} | SELL {quantity} @ ${exit_price:.2f}\n"
            f"PnL: {pnl_pct:+.2f}%\n"
            f"Reason: {exit_reason or 'signal_exit'}\n"
            f"Hold: {hold_days} days"
        )
        return True

    except Exception as exc:
        log.error("EXIT FAILED | %s | %s", symbol, exc)
        send_telegram(f"\U0001f534 *SYNK EXIT FAILED* | {symbol} | {exc}")
        return False


# ---------------------------------------------------------------------------
# Position monitor — momentum-based exit check
# ---------------------------------------------------------------------------
def check_exits(cfg: object) -> None:
    """
    Re-evaluate momentum gate for every expected open position.
    Submits a market sell for any position where momentum has flipped CLOSED.
    Updates data/expected_positions.json after exits.
    Never raises.
    """
    from strategy.synk_strategy import (  # noqa: PLC0415
        load_expected_positions,
        save_expected_positions,
    )
    from signals.momentum import get_latest_momentum, is_gate_open as momentum_gate_open  # noqa: PLC0415
    from data.price_feed import get_prices  # noqa: PLC0415
    from alpaca.trading.client import TradingClient  # noqa: PLC0415

    try:
        expected = load_expected_positions()
        if not expected:
            log.debug("check_exits: no expected positions — nothing to check")
            return

        client = TradingClient(  # type: ignore[attr-defined]
            cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY, paper=cfg.PAPER  # type: ignore[attr-defined]
        )

        try:
            alpaca_positions = {p.symbol: p for p in client.get_all_positions()}
        except Exception as exc:
            log.error("check_exits: could not fetch Alpaca positions: %s", exc)
            return

        # Only act on symbols that are both expected AND currently open in Alpaca.
        # Exclude the defence-beta sleeve — it is held outside gated logic and must
        # never be momentum/stop-exited by this loop (belt-and-braces: it should
        # not be in `expected` either, since job_strategy carves it out).
        sleeve_symbol = getattr(cfg, "SLEEVE_SYMBOL", "PPA")
        open_symbols = (expected & set(alpaca_positions.keys())) - {sleeve_symbol}
        if not open_symbols:
            log.debug("check_exits: expected positions not found in Alpaca — skipping")
            return

        prices = get_prices(list(open_symbols))
        exited: set[str] = set()

        for symbol in open_symbols:
            pos = alpaca_positions[symbol]
            quantity = int(float(pos.qty))
            if quantity <= 0:
                continue

            # --- Stop loss (hard limit — checked first, takes priority) ---
            unrealised_plpc = float(pos.unrealized_plpc or 0)
            if unrealised_plpc <= -_STOP_LOSS_POSITION_PCT:
                log.warning(
                    "check_exits: STOP LOSS | %s | unrealised=%.2f%% — exiting "
                    "(limit: -%.0f%%)",
                    symbol, unrealised_plpc * 100, _STOP_LOSS_POSITION_PCT * 100,
                )
                _current_price = float(pos.current_price or 0)
                _loss_pct = abs(unrealised_plpc * 100)
                send_telegram(
                    f"\U0001f6d1 *SYNK STOP LOSS*\n"
                    f"{symbol} | SELL {quantity} @ ${_current_price:.2f}\n"
                    f"Loss: {_loss_pct:.2f}% (threshold: -8%)"
                )
                if submit_exit(symbol, quantity, cfg, exit_reason="stop_loss"):
                    exited.add(symbol)
                continue  # stop fired — skip remaining checks

            # --- Price data for all remaining checks ---
            price_df = prices.get(symbol)
            if price_df is None or price_df.empty:
                log.warning("check_exits: no price data for %s — skipping", symbol)
                continue

            # --- Safe-haven hostile regime exit (GLD only) ---
            if symbol == "GLD":
                from signals.regime_filter import check_safe_haven_confirmation  # noqa: PLC0415
                sh_result = check_safe_haven_confirmation(price_df)
                if not sh_result["confirmed"]:
                    log.warning(
                        "check_exits: SH_HOSTILE_REGIME | GLD | return_5d=%.2f%% — exiting",
                        sh_result["return_5d"] * 100,
                    )
                    send_telegram(
                        f"\U0001f6d1 *SYNK EXIT — SH_HOSTILE_REGIME*\n"
                        f"GLD | return_5d={sh_result['return_5d']:+.2%}\n"
                        f"Gold falling in high-GPR regime — exiting position"
                    )
                    if submit_exit(symbol, quantity, cfg, exit_reason="SH_HOSTILE_REGIME"):
                        exited.add(symbol)
                    continue  # hostile regime — skip momentum check

            # --- Momentum gate (signal-based exit) ---
            try:
                momentum = get_latest_momentum(symbol, price_df)
            except Exception as exc:
                log.error("check_exits: momentum error for %s: %s", symbol, exc)
                continue

            if not momentum_gate_open(momentum):
                log.info(
                    "check_exits: MOMENTUM CLOSED | %s | roc=%+.4f close=%.2f "
                    "sma=%.2f — exiting",
                    symbol, momentum.roc_20, momentum.close, momentum.sma_20,
                )
                if submit_exit(symbol, quantity, cfg, exit_reason="momentum_closed"):
                    exited.add(symbol)

        if exited:
            save_expected_positions(expected - exited)
            log.info(
                "check_exits: exited %s | remaining open: %s",
                sorted(exited), sorted(expected - exited),
            )

    except Exception as exc:
        log.error("check_exits failed: %s", exc)

"""
sleeve_test

Pure unit tests for the defence-sleeve rebalance decision logic
(execution.sleeve_executor.compute_rebalance). No network, no Alpaca, no IO.

Run from synk/ root:
    python tests/sleeve_test.py

Exit code 0 = all pass, 1 = a case failed.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SYNK_ROOT = Path(__file__).resolve().parent.parent
if str(_SYNK_ROOT) not in sys.path:
    sys.path.insert(0, str(_SYNK_ROOT))

from execution.sleeve_executor import compute_rebalance

_EQUITY = 100_000.0
_TARGET = 0.15
_BAND = 0.03
_PRICE = 100.0


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "OK " if cond else "XX "
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    return cond


def main() -> int:
    passed = True

    # 1. Zero position (initial seed) -> BUY full target notional
    d = compute_rebalance(_EQUITY, 0.0, _PRICE, _TARGET, _BAND)
    passed &= _check(
        "seed: no position -> BUY target",
        d["action"] == "BUY" and abs(d["notional"] - 15_000.0) < 1e-6,
        f"action={d['action']} notional={d['notional']}",
    )

    # 2. Underweight beyond band -> BUY the shortfall
    #    sleeve at 10% (10k) vs target 15% (15k) -> drift -5% (> band) -> BUY 5k
    d = compute_rebalance(_EQUITY, 10_000.0, _PRICE, _TARGET, _BAND)
    passed &= _check(
        "underweight -> BUY shortfall",
        d["action"] == "BUY" and abs(d["notional"] - 5_000.0) < 1e-6,
        f"action={d['action']} notional={d['notional']}",
    )

    # 3. Overweight beyond band -> SELL whole shares
    #    sleeve at 20% (20k) vs target 15% (15k) -> drift +5% -> SELL ~5k / $100 = 50 sh
    d = compute_rebalance(_EQUITY, 20_000.0, _PRICE, _TARGET, _BAND)
    passed &= _check(
        "overweight -> SELL whole shares",
        d["action"] == "SELL" and d["qty"] == 50,
        f"action={d['action']} qty={d['qty']}",
    )

    # 4. Within band -> HOLD
    #    sleeve at 16% (16k) vs target 15% -> drift +1% (< 3% band) -> HOLD
    d = compute_rebalance(_EQUITY, 16_000.0, _PRICE, _TARGET, _BAND)
    passed &= _check(
        "within band -> HOLD",
        d["action"] == "HOLD",
        f"action={d['action']} drift={d['drift']}",
    )

    # 5. Just past band but dust delta -> HOLD
    #    Use a tiny account so a >band drift produces a sub-$50 delta.
    #    equity=$500, target 15% = $75; sleeve=$35 -> w=7%, drift -8% (> band),
    #    delta=$40 (< $50 min) -> HOLD.
    d = compute_rebalance(500.0, 35.0, _PRICE, _TARGET, _BAND)
    passed &= _check(
        "dust delta -> HOLD",
        d["action"] == "HOLD" and "dust" in d["reason"],
        f"action={d['action']} reason={d['reason']}",
    )

    # 6. Non-positive equity -> HOLD (guard)
    d = compute_rebalance(0.0, 0.0, _PRICE, _TARGET, _BAND)
    passed &= _check(
        "zero equity -> HOLD",
        d["action"] == "HOLD",
        f"action={d['action']}",
    )

    # 7. Overweight but < 1 share to sell -> HOLD
    #    Make a >band overweight whose dollar delta exceeds the dust floor but is
    #    less than one share. equity=$100k, target 15% = $15k; price very high so
    #    delta < price. sleeve=$15,060 -> drift +0.06% < band... need > band.
    #    Use price=20000: target 15k, sleeve=18,100 -> drift +3.1% (> band),
    #    delta=-3,100, qty=int(3100/20000)=0 -> HOLD.
    d = compute_rebalance(_EQUITY, 18_100.0, 20_000.0, _TARGET, _BAND)
    passed &= _check(
        "overweight < 1 share -> HOLD",
        d["action"] == "HOLD" and "1 share" in d["reason"],
        f"action={d['action']} reason={d['reason']}",
    )

    print()
    if passed:
        print("All sleeve rebalance tests passed.")
        return 0
    print("FAILURES detected.")
    return 1


if __name__ == "__main__":
    print("\n=== SLEEVE REBALANCE DECISION TESTS ===")
    raise SystemExit(main())

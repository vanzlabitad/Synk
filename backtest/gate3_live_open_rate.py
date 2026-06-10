"""
gate3_live_open_rate

Diagnostic for the Gate 3 (sentiment) keep/drop decision (SESSION_2026-05-30,
open decision #1). Read-only — writes nothing, sends nothing.

Answers two questions:
    1. What is the gate's ACTUAL open rate on live GDELT data
       (logs/sentiment_cache.jsonl), versus the ~55% the May 30 retune
       (p55/s20) showed on the historical backtest distribution?
    2. Has the live FinBERT output distribution (dominant_prob,
       sentiment_score) shifted relative to the 2020-2026 backtest parquet?

Usage:
    python backtest/gate3_live_open_rate.py

Outputs (console):
    - live cache stats: n, span, open rate, class mix, prob/score mean +- SD
    - open-rate grid across the same 16 threshold combos as the backtest
    - live vs historical distribution comparison with Cohen's d
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve().parent.parent  # synk/ root
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_LIVE_JSONL = _HERE / "logs" / "sentiment_cache.jsonl"
_HIST_PARQUET = (
    _HERE / "backtest" / "results" / "historical_sentiment_2020-01-01_2026-01-01.parquet"
)

# Same grid as backtest/historical_sentiment.py gate_at_* columns
_PROB_THRESHOLDS = (0.45, 0.50, 0.55, 0.60)
_SCORE_THRESHOLDS = (0.10, 0.15, 0.20, 0.30)
_LIVE_P, _LIVE_S = 0.55, 0.20  # live stack thresholds (signals/sentiment.py)


def _load_live(path: Path = _LIVE_JSONL) -> pd.DataFrame:
    """Load the live sentiment cache jsonl into a DataFrame."""
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    df = pd.DataFrame(records)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def _open_rate(df: pd.DataFrame, p: float, s: float) -> float:
    """Gate-open fraction at thresholds p/s (same logic as signals/sentiment.py)."""
    gate = (df["dominant_prob"] > p) & (df["sentiment_score"].abs() > s)
    return float(gate.mean())


def _cohens_d(a: pd.Series, b: pd.Series) -> float:
    """Cohen's d with pooled SD (descriptive; live n is small)."""
    na, nb = len(a), len(b)
    pooled_var = ((na - 1) * a.var(ddof=1) + (nb - 1) * b.var(ddof=1)) / (na + nb - 2)
    return float((a.mean() - b.mean()) / pooled_var**0.5) if pooled_var > 0 else 0.0


def main() -> None:
    live = _load_live()
    hist = pd.read_parquet(_HIST_PARQUET)

    n_live, n_hist = len(live), len(hist)
    span = f"{live['timestamp'].min():%Y-%m-%d} .. {live['timestamp'].max():%Y-%m-%d}"

    print("=" * 72)
    print("GATE 3 (SENTIMENT) LIVE DIAGNOSTIC")
    print("=" * 72)

    print(f"\n--- Live cache: {_LIVE_JSONL.name} ---")
    print(f"n = {n_live} cycles | span: {span}")
    print(f"Recorded gate-open rate:        {live['signal'].mean():.1%}")
    print(f"Recomputed at p{_LIVE_P}/s{_LIVE_S}:  {_open_rate(live, _LIVE_P, _LIVE_S):.1%}")
    print(f"Dominant class mix: {live['dominant_class'].value_counts(normalize=True).round(3).to_dict()}")
    print(f"dominant_prob:   mean {live['dominant_prob'].mean():.4f} +- SD {live['dominant_prob'].std(ddof=1):.4f}")
    print(f"sentiment_score: mean {live['sentiment_score'].mean():+.4f} +- SD {live['sentiment_score'].std(ddof=1):.4f}")

    print(f"\n--- Historical parquet: {_HIST_PARQUET.name} ---")
    print(f"n = {n_hist} days | span: {hist['date'].min():%Y-%m-%d} .. {hist['date'].max():%Y-%m-%d}")
    print(f"Dominant class mix: {hist['dominant_class'].value_counts(normalize=True).round(3).to_dict()}")
    print(f"dominant_prob:   mean {hist['dominant_prob'].mean():.4f} +- SD {hist['dominant_prob'].std(ddof=1):.4f}")
    print(f"sentiment_score: mean {hist['sentiment_score'].mean():+.4f} +- SD {hist['sentiment_score'].std(ddof=1):.4f}")

    print("\n--- Open-rate grid: live (historical) ---")
    header = "p \\ s   " + "".join(f"{s:>16.2f}" for s in _SCORE_THRESHOLDS)
    print(header)
    for p in _PROB_THRESHOLDS:
        cells = []
        for s in _SCORE_THRESHOLDS:
            live_rate = _open_rate(live, p, s)
            col = f"gate_at_p{int(p * 100)}_s{int(s * 100)}"
            hist_rate = float(hist[col].mean()) if col in hist.columns else float("nan")
            mark = " <" if (p, s) == (_LIVE_P, _LIVE_S) else "  "
            cells.append(f"{live_rate:>6.1%} ({hist_rate:.1%}){mark}")
        print(f"{p:.2f}  " + "".join(f"{c:>16}" for c in cells))
    print("(< marks the live stack thresholds)")

    print("\n--- Distribution shift: live vs historical ---")
    d_prob = _cohens_d(live["dominant_prob"], hist["dominant_prob"])
    d_score = _cohens_d(live["sentiment_score"], hist["sentiment_score"])
    print(f"dominant_prob:   diff {live['dominant_prob'].mean() - hist['dominant_prob'].mean():+.4f} | Cohen's d = {d_prob:+.3f}")
    print(f"sentiment_score: diff {live['sentiment_score'].mean() - hist['sentiment_score'].mean():+.4f} | Cohen's d = {d_score:+.3f}")

    print(
        f"\nNOTE: descriptive comparison only — live n={n_live} hourly cycles vs "
        f"historical n={n_hist} daily aggregates (different sampling units; no "
        "inferential test run). Use the grid + shift to decide keep/drop/retune."
    )


if __name__ == "__main__":
    main()

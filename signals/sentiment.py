"""
sentiment

Gate 3 of the Synk three-factor signal stack: FinBERT sentiment scoring.

Architecture: pre-compute + cache (NOT inline).
    FinBERT warm load is ~25s on CPU. Calling it inside the strategy loop
    would block every evaluation. Instead, this module runs on a schedule
    (hourly, matching the GDELT fetch interval), writes results to
    logs/sentiment_cache.jsonl, and the strategy reads the latest cached entry.

Signal logic:
    1. Fetch the latest GDELT GKG file (via data.gdeltproject.org — same host
       as gdelt_fetcher.py, known reachable).
    2. Filter articles where V2Tone < _CONFLICT_TONE_THRESHOLD (conflict proxy
       for Goldstein-scale tension) AND Themes contains a conflict keyword.
    3. Extract headline text: GKG Quotations field when non-empty, else URL slug.
    4. Run ProsusAI/finbert on the headline batch.
    5. Aggregate per-headline probabilities:
         dominant_class = argmax(mean probability across all headlines)
         sentiment_score = mean(positive_prob) - mean(negative_prob)
    6. Gate opens when dominant_prob > 0.55 AND abs(sentiment_score) > 0.20.
    7. Append SentimentSignal to logs/sentiment_cache.jsonl.

Usage (standalone — runs one scheduled cycle):
    python signals/sentiment.py

Benchmarks (2026-04-22, CPU-only):
    Warm model load: ~25s   — acceptable for hourly job, not for inline calls
    Inference 15 headlines: ~0.45s (30ms each)

Deps: transformers, torch, requests, pandas
Run from synk/ root.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import sys
import time
import zipfile

# GKG fields can exceed the 131072-byte default; raise the limit once at import
csv.field_size_limit(sys.maxsize)
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent.parent  # synk/ root
_LOG_DIR = _HERE / "logs"
_SENTIMENT_JSONL = _LOG_DIR / "sentiment_cache.jsonl"

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# ---------------------------------------------------------------------------
# Tuneable constants
# ---------------------------------------------------------------------------
_MODEL_NAME = "ProsusAI/finbert"
_MAX_HEADLINES = 50              # cap to keep inference time bounded
_BATCH_SIZE = 16                 # FinBERT batch size (fits comfortably in RAM)
_MAX_TOKEN_LENGTH = 512          # BERT max sequence length

_DOMINANT_PROB_THRESHOLD = 0.55  # gate condition 1
_SENTIMENT_SCORE_THRESHOLD = 0.20  # gate condition 2: abs(sentiment_score) > this

# GDELT headline sourcing
_MASTERFILE_URL = "http://data.gdeltproject.org/gdeltv2/masterfilelist.txt"
_MASTERFILE_TAIL_BYTES = 2000
_FETCH_TIMEOUT = 20              # seconds per HTTP request

# Conflict filter: V2Tone below this is treated as tension/conflict signal
# Mirrors the Goldstein-scale negativity in the GPR signal stack
_CONFLICT_TONE_THRESHOLD = -2.0

# GKG theme keywords that indicate geopolitical conflict
_CONFLICT_THEMES = frozenset({
    "MILITARY", "CRISISLEX", "WAR", "TERROR", "SANCTIONS",
    "WB_696_CONFLICT", "WEAPONS", "MANMADE_DISASTER",
})

# ---------------------------------------------------------------------------
# Logging — stdout + logs/process.log, UTC timestamps
# ---------------------------------------------------------------------------
def _setup_logging() -> logging.Logger:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("sentiment")
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
class SentimentSignal:
    timestamp: str          # ISO 8601 UTC of when the signal was computed
    headline_count: int     # number of headlines processed
    dominant_class: str     # 'positive', 'negative', or 'neutral'
    dominant_prob: float    # mean probability of dominant class across headlines
    sentiment_score: float  # mean(positive_prob) - mean(negative_prob), range [-1, 1]
    signal: bool            # True = gate open

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# FinBERT pipeline — lazy load, cached at module level after first call
# ---------------------------------------------------------------------------
_pipeline = None


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        import os
        # tqdm calls sys.stdout.isatty(); Windows Task Scheduler provides no console
        os.environ.setdefault("TQDM_DISABLE", "1")
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w")
        # Load .env so HF_TOKEN is available even on standalone runs (idempotent;
        # won't clobber env vars already set by the main entrypoint).
        from dotenv import load_dotenv  # noqa: PLC0415
        load_dotenv()
        # Silence the transformers weight-load report (the harmless
        # "bert.embeddings.position_ids | UNEXPECTED" table) — keep ERROR so
        # genuine load failures still surface.
        from transformers.utils import logging as hf_logging  # noqa: PLC0415
        hf_logging.set_verbosity_error()
        from transformers import pipeline as hf_pipeline  # noqa: PLC0415
        # token=None is the anonymous default; a real HF_TOKEN authenticates the
        # download/metadata check, avoiding HF rate-limit warnings on cold start.
        hf_token = os.environ.get("HF_TOKEN") or None
        log.info("Loading FinBERT model: %s (auth=%s, first call, ~25s on CPU)",
                 _MODEL_NAME, "yes" if hf_token else "no")
        t0 = time.time()
        _pipeline = hf_pipeline(
            "text-classification",
            model=_MODEL_NAME,
            top_k=None,
            truncation=True,
            max_length=_MAX_TOKEN_LENGTH,
            token=hf_token,
        )
        log.info("FinBERT loaded in %.1fs", time.time() - t0)
    return _pipeline


# ---------------------------------------------------------------------------
# GDELT headline sourcing
# ---------------------------------------------------------------------------
def _url_to_slug(url: str) -> str:
    """Extract a readable slug from a news article URL as a headline proxy."""
    try:
        path = urlparse(url).path.rstrip("/")
        slug = path.split("/")[-1]
        # Strip common file extensions
        if "." in slug[-5:]:
            slug = slug.rsplit(".", 1)[0]
        return slug.replace("-", " ").replace("_", " ").strip()
    except Exception:
        return ""


def _parse_tone(tone_field: str) -> float | None:
    """Parse the first (overall) score from a GDELT V2Tone field."""
    try:
        return float(tone_field.split(",")[0])
    except (ValueError, IndexError):
        return None


def _has_conflict_theme(themes_field: str) -> bool:
    """Return True if any conflict keyword appears in the GKG Themes column."""
    upper = themes_field.upper()
    return any(kw in upper for kw in _CONFLICT_THEMES)


def fetch_gdelt_headlines(max_articles: int = _MAX_HEADLINES) -> list[str]:
    """
    Download the latest GDELT GKG file and return headline strings for
    conflict-themed articles.

    Headline priority:
        1. GKG Quotations field (real extracted text from the article)
        2. URL path slug (cleaned, used as a title proxy when no quote exists)

    Filters applied:
        - V2Tone < _CONFLICT_TONE_THRESHOLD  (conflict/tension proxy)
        - At least one _CONFLICT_THEMES keyword in Themes column

    Returns an empty list on network failure (caller handles gracefully).
    GKG column layout (0-indexed, tab-delimited):
        4  = DocumentIdentifier (URL)
        7  = Themes
        15 = V2Tone
        22 = Quotations
    """
    try:
        # Find the latest GKG file URL from the masterfile tail
        resp = requests.get(
            _MASTERFILE_URL,
            headers={"Range": f"bytes=-{_MASTERFILE_TAIL_BYTES}"},
            timeout=_FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        gkg_url = next(
            (line.split()[2] for line in reversed(resp.text.strip().splitlines())
             if "gkg.csv.zip" in line and len(line.split()) >= 3),
            None,
        )
        if not gkg_url:
            log.warning("No GKG file found in masterfile tail")
            return []

        log.info("Fetching GKG: %s", gkg_url)
        r2 = requests.get(gkg_url, timeout=_FETCH_TIMEOUT)
        r2.raise_for_status()

        headlines: list[str] = []
        with zipfile.ZipFile(io.BytesIO(r2.content)) as z:
            with z.open(z.namelist()[0]) as f:
                reader = csv.reader(
                    io.TextIOWrapper(f, encoding="utf-8", errors="replace"),
                    delimiter="\t",
                )
                for row in reader:
                    if len(row) < 23:
                        continue

                    url = row[4]
                    themes = row[7]
                    tone_field = row[15]
                    quotations = row[22]

                    # Apply conflict filters
                    tone = _parse_tone(tone_field)
                    if tone is None or tone >= _CONFLICT_TONE_THRESHOLD:
                        continue
                    if not _has_conflict_theme(themes):
                        continue

                    # Prefer quoted text; fall back to URL slug
                    if quotations.strip():
                        # Quotations format: CHAROFFSET|LEN||TEXT#...
                        # Extract the text portion of the first quotation
                        first_quote = quotations.split("#")[0]
                        parts = first_quote.split("||", 1)
                        text = parts[1].strip() if len(parts) > 1 else ""
                    else:
                        text = _url_to_slug(url)

                    if len(text) > 10:  # skip trivially short fragments
                        headlines.append(text)

                    if len(headlines) >= max_articles:
                        break

        log.info("Fetched %d conflict headlines from GKG", len(headlines))
        return headlines

    except Exception as exc:
        log.error("Failed to fetch GDELT headlines: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Sentiment scoring
# ---------------------------------------------------------------------------
def score_headlines(headlines: list[str]) -> dict[str, float]:
    """
    Run FinBERT on a list of headline strings.

    Returns a dict with mean probabilities for each class:
        {'positive': float, 'negative': float, 'neutral': float}

    Raises ValueError if headlines is empty.
    """
    if not headlines:
        raise ValueError("Cannot score an empty headline list.")

    pipe = _get_pipeline()
    log.info("Scoring %d headlines (batch_size=%d)", len(headlines), _BATCH_SIZE)
    t0 = time.time()
    results = pipe(headlines, batch_size=_BATCH_SIZE)
    elapsed = time.time() - t0
    log.info("FinBERT inference done in %.2fs (%.0fms/headline)", elapsed, elapsed / len(headlines) * 1000)

    # Accumulate probabilities across all headlines
    totals: dict[str, float] = {"positive": 0.0, "negative": 0.0, "neutral": 0.0}
    for per_headline in results:
        for entry in per_headline:
            label = entry["label"].lower()
            if label in totals:
                totals[label] += entry["score"]

    n = len(headlines)
    return {k: v / n for k, v in totals.items()}


def build_signal(
    mean_probs: dict[str, float],
    headline_count: int,
) -> SentimentSignal:
    """
    Convert aggregated FinBERT probabilities into a SentimentSignal.

    dominant_class  = class with highest mean probability
    dominant_prob   = mean probability of that class
    sentiment_score = mean_positive - mean_negative  (range approx [-1, 1])
    signal          = dominant_prob > threshold AND abs(sentiment_score) > threshold
    """
    dominant_class = max(mean_probs, key=mean_probs.__getitem__)
    dominant_prob = mean_probs[dominant_class]
    sentiment_score = mean_probs["positive"] - mean_probs["negative"]

    gate = (
        dominant_prob > _DOMINANT_PROB_THRESHOLD
        and abs(sentiment_score) > _SENTIMENT_SCORE_THRESHOLD
    )

    return SentimentSignal(
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        headline_count=headline_count,
        dominant_class=dominant_class,
        dominant_prob=round(dominant_prob, 4),
        sentiment_score=round(sentiment_score, 4),
        signal=gate,
    )


# ---------------------------------------------------------------------------
# Public API — called by synk_strategy.py
# ---------------------------------------------------------------------------
def run_sentiment_cycle(max_articles: int = _MAX_HEADLINES) -> SentimentSignal:
    """
    Fetch GDELT headlines, score with FinBERT, return a SentimentSignal.

    If fewer than 5 conflict headlines are found, returns a NEUTRAL non-signal
    rather than scoring on insufficient data.
    """
    headlines = fetch_gdelt_headlines(max_articles)

    if len(headlines) < 5:
        log.warning(
            "Only %d conflict headlines found (need >= 5) — returning neutral non-signal",
            len(headlines),
        )
        return SentimentSignal(
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            headline_count=len(headlines),
            dominant_class="neutral",
            dominant_prob=0.0,
            sentiment_score=0.0,
            signal=False,
        )

    mean_probs = score_headlines(headlines)
    signal = build_signal(mean_probs, len(headlines))

    log.info(
        "Sentiment: %s (prob=%.3f, score=%+.3f) | gate=%s | n=%d",
        signal.dominant_class,
        signal.dominant_prob,
        signal.sentiment_score,
        "OPEN" if signal.signal else "CLOSED",
        signal.headline_count,
    )
    return signal


def get_latest_cached() -> SentimentSignal | None:
    """
    Read the most recent SentimentSignal from sentiment_cache.jsonl.
    Returns None if the cache is empty or missing — strategy treats as CLOSED.
    """
    if not _SENTIMENT_JSONL.exists():
        return None
    with open(_SENTIMENT_JSONL, encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]
    if not lines:
        return None
    d = json.loads(lines[-1])
    d.pop("logged_utc", None)
    return SentimentSignal(**d)


def is_gate_open(signal: SentimentSignal) -> bool:
    """True when dominant_prob > 0.55 AND abs(sentiment_score) > 0.20."""
    return signal.signal


# ---------------------------------------------------------------------------
# JSONL persistence
# ---------------------------------------------------------------------------
def append_to_jsonl(signal: SentimentSignal, path: Path = _SENTIMENT_JSONL) -> None:
    """Append a SentimentSignal as a newline-delimited JSON record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = signal.as_dict()
    record["logged_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    log.info("Sentiment signal appended to %s", path)


# ---------------------------------------------------------------------------
# Entry point — one scheduled cycle: fetch, score, print, cache
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    signal = run_sentiment_cycle()

    print("\n--- Sentiment Signal ---")
    print(f"Timestamp:       {signal.timestamp}")
    print(f"Headlines scored:{signal.headline_count}")
    print(f"Dominant class:  {signal.dominant_class}")
    print(f"Dominant prob:   {signal.dominant_prob:.4f}  (threshold: >{_DOMINANT_PROB_THRESHOLD})")
    print(f"Sentiment score: {signal.sentiment_score:+.4f} (threshold: abs > {_SENTIMENT_SCORE_THRESHOLD})")
    print(f"Gate:            {'OPEN' if signal.signal else 'CLOSED'}")

    append_to_jsonl(signal)

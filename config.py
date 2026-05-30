"""
config

Loads environment variables from .env and exposes them as a typed Config
dataclass. Call get_config() at startup; it validates required keys and raises
EnvironmentError with a clear message if anything is missing.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass
class Config:
    ALPACA_API_KEY: str = ""
    ALPACA_SECRET_KEY: str = ""
    PAPER: bool = True
    LOG_DIR: str = "logs/"

    # --- Defence-beta sleeve (separate buy-and-hold tilt; see execution/sleeve_executor.py) ---
    # Ships DISABLED. Bound to PAPER. Enabling live + funding is a manual operator step.
    SLEEVE_ENABLED: bool = False        # master switch
    SLEEVE_SYMBOL: str = "PPA"          # defence-ETF vehicle
    SLEEVE_TARGET_WEIGHT: float = 0.15  # target % of total account equity (0.15-0.20 band chosen)
    SLEEVE_DRIFT_BAND: float = 0.03     # rebalance only if |w_current - w_target| > this
    SLEEVE_REBALANCE_DAYS: int = 90     # quarterly cadence gate (min days between rebalances)

    def __post_init__(self) -> None:
        load_dotenv()
        self.ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
        self.ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
        raw_paper = os.environ.get("ALPACA_PAPER", "true").lower()
        self.PAPER = raw_paper not in ("false", "0", "no")
        self.LOG_DIR = os.environ.get("LOG_DIR", "logs/")

        # Sleeve settings (all optional; safe defaults keep it disabled)
        raw_sleeve = os.environ.get("SLEEVE_ENABLED", "false").lower()
        self.SLEEVE_ENABLED = raw_sleeve in ("true", "1", "yes")
        self.SLEEVE_SYMBOL = os.environ.get("SLEEVE_SYMBOL", "PPA").upper()
        self.SLEEVE_TARGET_WEIGHT = float(os.environ.get("SLEEVE_TARGET_WEIGHT", "0.15"))
        self.SLEEVE_DRIFT_BAND = float(os.environ.get("SLEEVE_DRIFT_BAND", "0.03"))
        self.SLEEVE_REBALANCE_DAYS = int(os.environ.get("SLEEVE_REBALANCE_DAYS", "90"))

    def validate(self) -> list[str]:
        """Return a list of error strings. Empty list means config is valid."""
        errors: list[str] = []
        if not self.ALPACA_API_KEY:
            errors.append("ALPACA_API_KEY is missing or empty")
        if not self.ALPACA_SECRET_KEY:
            errors.append("ALPACA_SECRET_KEY is missing or empty")
        return errors


def get_config() -> Config:
    """Create and validate Config. Raises EnvironmentError if invalid."""
    cfg = Config()
    errors = cfg.validate()
    if errors:
        raise EnvironmentError(
            "Synk config validation failed:\n  " + "\n  ".join(errors)
        )
    return cfg

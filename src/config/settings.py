"""
Application settings — loads src/config/configuration.json into a typed
Settings dataclass.

Config file location:
    src/config/configuration.json   (git-ignored, never committed)
    src/config/configuration.json.example  (safe placeholder, committed)

Required fields:
    db_url              PostgreSQL connection string
    marketdata_token    Marketstack API access key

Optional fields:
    days                Days of history to fetch on incremental updates (default 1000)
    api_sleep_seconds   Delay between Marketstack API calls (default 0.2)
    anthropic_api_key   Anthropic API key for AI sector classification
    marketaux_token     Marketaux API key for the per-symbol news feed

Usage:
    from src.config.settings import load_settings
    settings = load_settings()
    print(settings.db_url)
"""
from dataclasses import dataclass
from pathlib import Path
import json


# Repo-relative config location:
#   src/config/configuration.json
CONFIG_FILE = Path(__file__).parent / "configuration.json"


@dataclass(frozen=True)
class Settings:
    db_url: str
    marketdata_token: str
    days: int
    api_sleep_seconds: float
    anthropic_api_key: str = ""  # optional — used for AI sector classification
    marketaux_token: str = ""    # optional — used for News tab headlines


def _load_from_file() -> dict:
    if not CONFIG_FILE.exists():
        raise RuntimeError(
            f"Missing config file: {CONFIG_FILE}\n"
            "Create src/config/config.json (see example below)."
        )

    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON in {CONFIG_FILE}: {e}") from e


def load_settings() -> Settings:
    cfg = _load_from_file()

    try:
        return Settings(
            db_url=cfg["db_url"],
            marketdata_token=cfg["marketdata_token"],
            days=int(cfg.get("days", 1000)),
            api_sleep_seconds=float(cfg.get("api_sleep_seconds", 0.2)),
            anthropic_api_key=cfg.get("anthropic_api_key", ""),
            marketaux_token=cfg.get("marketaux_token", ""),
        )
    except KeyError as e:
        raise RuntimeError(f"Missing required config key: {e}") from e

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
        )
    except KeyError as e:
        raise RuntimeError(f"Missing required config key: {e}") from e

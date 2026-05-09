"""
Application settings — loads config from one of three sources, in priority order:

    1. st.secrets               (Streamlit Community Cloud — set via app UI)
    2. Environment variables    (GitHub Actions — set via repo secrets)
    3. configuration.json       (local dev — git-ignored, never committed)

The first source that has a non-empty `db_url` wins. This lets the same code
run locally (JSON file), on Streamlit Cloud (st.secrets), and inside a
scheduled GHA daily-update job (env vars) without changes.

Config file location (local only):
    src/config/configuration.json           (git-ignored)
    src/config/configuration.json.example   (safe placeholder, committed)

Required fields:
    db_url              PostgreSQL connection string
    marketdata_token    Marketstack API access key

Optional fields:
    days                Days of history to fetch on incremental updates (default 1000)
    api_sleep_seconds   Delay between Marketstack API calls (default 0.2)
    anthropic_api_key   Anthropic API key for AI sector classification + Ask tab
    anthropic_model     Claude model id for the Ask tab (default "claude-haiku-4-5")
    db_url_readonly     Postgres connection string for the readonly role used
                        by the Ask tab to execute AI-generated SQL
    marketaux_token     Marketaux API key for the per-symbol news feed

Environment variable names (uppercase of each field):
    DB_URL, MARKETDATA_TOKEN, DAYS, API_SLEEP_SECONDS,
    ANTHROPIC_API_KEY, ANTHROPIC_MODEL, DB_URL_READONLY, MARKETAUX_TOKEN

Usage:
    from src.config.settings import load_settings
    settings = load_settings()
    print(settings.db_url)
"""
from dataclasses import dataclass
from pathlib import Path
import json
import os


# Repo-relative config location:
#   src/config/configuration.json
CONFIG_FILE = Path(__file__).parent / "configuration.json"

# Required + optional field names. Keep in sync with the Settings dataclass.
_FIELDS: tuple[str, ...] = (
    "db_url",
    "marketdata_token",
    "days",
    "api_sleep_seconds",
    "anthropic_api_key",
    "anthropic_model",
    "db_url_readonly",
    "marketaux_token",
)


@dataclass(frozen=True)
class Settings:
    db_url: str
    marketdata_token: str
    days: int
    api_sleep_seconds: float
    anthropic_api_key: str = ""           # AI sector classification + Ask tab
    anthropic_model: str = "claude-haiku-4-5"  # Claude model id used by the Ask tab (Haiku — fastest/cheapest tier)
    db_url_readonly: str = ""             # Readonly role for AI-generated SQL
    marketaux_token: str = ""             # News tab headlines


def _load_from_streamlit_secrets() -> dict | None:
    """
    Try to read config from Streamlit's secrets manager. Returns None if
    Streamlit isn't installed, isn't running, or has no secrets configured.

    On Streamlit Community Cloud, secrets are entered via the app's settings
    UI and surface as `st.secrets["key"]`. We accept both flat keys and a
    `[market_atlas]` table for namespacing.
    """
    try:
        import streamlit as st  # noqa: WPS433  (deferred import — optional source)
    except ImportError:
        return None

    # Streamlit raises StreamlitSecretNotFoundError on any access (including
    # `in`) when no secrets.toml exists locally. Wrap the entire read so the
    # function silently falls through to the next source instead of crashing.
    try:
        secrets = st.secrets
        section: dict = {}
        if "market_atlas" in secrets:
            section = dict(secrets["market_atlas"])

        cfg: dict = {}
        for field in _FIELDS:
            if field in section:
                cfg[field] = section[field]
            elif field in secrets:
                cfg[field] = secrets[field]
    except Exception:
        return None

    return cfg if cfg.get("db_url") else None


def _load_from_env() -> dict | None:
    """
    Read config from environment variables. Used by the GitHub Actions
    daily-update job, where secrets are injected as env vars. Returns None
    if `DB_URL` isn't set — the marker we use to decide that env-based
    config is intentional.
    """
    if not os.environ.get("DB_URL"):
        return None

    cfg: dict = {}
    for field in _FIELDS:
        value = os.environ.get(field.upper())
        if value is not None and value != "":
            cfg[field] = value
    return cfg


def _load_from_file() -> dict:
    if not CONFIG_FILE.exists():
        raise RuntimeError(
            f"Missing config file: {CONFIG_FILE}\n"
            "Create src/config/configuration.json from configuration.json.example,\n"
            "or set st.secrets (Streamlit Cloud) / env vars (GitHub Actions) instead."
        )

    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON in {CONFIG_FILE}: {e}") from e


def load_settings() -> Settings:
    """
    Load Settings from the first available source: Streamlit secrets, then
    environment variables, then the local JSON file.
    """
    cfg = _load_from_streamlit_secrets() or _load_from_env() or _load_from_file()

    try:
        return Settings(
            db_url=cfg["db_url"],
            marketdata_token=cfg["marketdata_token"],
            days=int(cfg.get("days", 1000)),
            api_sleep_seconds=float(cfg.get("api_sleep_seconds", 0.2)),
            anthropic_api_key=cfg.get("anthropic_api_key", "") or "",
            anthropic_model=cfg.get("anthropic_model", "") or "claude-haiku-4-5",
            db_url_readonly=cfg.get("db_url_readonly", "") or "",
            marketaux_token=cfg.get("marketaux_token", "") or "",
        )
    except KeyError as e:
        raise RuntimeError(f"Missing required config key: {e}") from e

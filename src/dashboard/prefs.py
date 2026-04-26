"""
Persistent UI preferences — survives browser tab close/refresh.

Storage: ~/.marketatlas/prefs.json  (created on first save, best-effort).
Sensitive data (db_url, tokens) is never written here.

Public API
----------
load_prefs() -> dict        Read disk → dict; returns {} on any error.
save_prefs(d: dict) -> None Atomic-ish write; silently ignores OSError.
"""
from __future__ import annotations

import json
from pathlib import Path

_PREFS_PATH = Path.home() / ".marketatlas" / "prefs.json"


def load_prefs() -> dict:
    try:
        return json.loads(_PREFS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_prefs(d: dict) -> None:
    try:
        _PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PREFS_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except OSError:
        pass

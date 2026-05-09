"""
Question → AI-decision cache for the Ask tab.

Caches two things separately, both keyed on the normalised question + model:

  1. The router's decision — which template the question maps to (with bound
     params) or "no match". This is what saves API cost on repeats.
  2. The free-form SQL the AI-SQL fallback generated — for questions the
     router couldn't route.

We deliberately do NOT cache database results. The cached entries hold only
the LLM's decision; the actual SQL still runs against the current DB state on
every query, so daily price/volume changes are picked up automatically.
Templates anchor "past N days" to the latest bar in daily_bars, so a cached
"30 days" routing today gives a different result after a fresh data refresh
— that's exactly what we want.

Cache scope
-----------
- Process-wide (module-level OrderedDict). Survives Streamlit reruns within
  the same server process; lost on restart. Acceptable: the cache is a cost
  optimisation, not a source of truth.
- Thread-safe via RLock. Streamlit can run multiple sessions concurrently
  in worker threads.
- LRU eviction at MAX_ENTRIES; per-entry TTL of CACHE_TTL_SECONDS.

Why not @st.cache_data?
-----------------------
@st.cache_data is great when you don't need to know whether a call hit or
missed. We want to surface that to the audit log (cache hits log token
counts as 0) and to the UI (a "cached" badge on history entries), so an
explicit cache with a callable hit-test is cleaner.
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Any


CACHE_TTL_SECONDS = 12 * 3600   # matches the existing news cache TTL
MAX_ENTRIES = 200


# ---------------------------------------------------------------------------
# TTLCache — generic LRU + TTL. Kept tiny on purpose.
# ---------------------------------------------------------------------------

class TTLCache:
    """
    Thread-safe LRU cache with per-entry TTL.

    Stored value must be picklable / serialisable in spirit (no live
    connections, sessions, etc.) — we only store small primitive values.
    """

    def __init__(self, max_entries: int = MAX_ENTRIES, ttl_seconds: int = CACHE_TTL_SECONDS):
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._max = max_entries
        self._ttl = ttl_seconds
        self._lock = RLock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Any | None:
        """Return cached value or None on miss / expiry. Updates LRU order."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            ts, value = entry
            if time.time() - ts > self._ttl:
                del self._cache[key]
                self._misses += 1
                return None
            self._cache.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._cache[key] = (time.time(), value)
            self._cache.move_to_end(key)
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)

    def invalidate(self, key: str) -> bool:
        with self._lock:
            return self._cache.pop(key, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict:
        with self._lock:
            return {
                "size": len(self._cache),
                "max_entries": self._max,
                "ttl_seconds": self._ttl,
                "hits": self._hits,
                "misses": self._misses,
            }


# ---------------------------------------------------------------------------
# Two singletons — one for routing decisions, one for AI-SQL fallback strings.
# Kept separate because their entries have different shapes; merging would
# force a tagged union and complicate type-checking.
# ---------------------------------------------------------------------------

_ROUTE_CACHE = TTLCache()
_AI_SQL_CACHE = TTLCache()


# Bump this whenever prompts in src/ai/nl_to_sql.py, src/ai/narrate.py, or
# templates in src/ai/query_templates.py change in a way that affects the
# SQL the LLM generates or the narrative summary it produces. The version
# is folded into both the SQL-cache key and the narrate-cache key so old
# stale entries become unreachable on the next deploy — no manual cache
# clear needed.
# History:
#   v1 (implicit, pre-version): original (ts AT TIME ZONE 'UTC')::date pattern.
#   v2 (2026-05-09): switched to b.ts >= NOW() - INTERVAL '...' for chunk
#       pruning; added 14-day filter on end_px CTEs.
#   v3 (2026-05-09): narrate prompt now refers to "tabs" explicitly
#       (e.g. "See the **Stock Detail** tab"); Index Overlap removed from
#       the tab list since that tab is hidden from the strip.
#   v4 (2026-05-09): conversational memory — last few turns are now
#       injected into router/SQL/narrate prompts so referential follow-ups
#       resolve. Fold a transcript hash into the cache key so the same
#       follow-up question ("their names") under different conversations
#       maps to different cache entries.
PROMPT_VERSION = "v4"


# ---------------------------------------------------------------------------
# Public API — typed payloads
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RouteCacheEntry:
    """
    Frozen, picklable representation of a router decision.

    `kind == "template"` → name + params populated.
    `kind == "miss"`     → no template matched; caller falls back to AI-SQL.
    """
    kind: str
    name: str | None = None
    params: dict | None = None


def _normalise(question: str) -> str:
    """Whitespace-collapse + lowercase. Punctuation is intentionally preserved
    — '10% gain in 30 days' and '10 percent gain' route to different params."""
    return re.sub(r"\s+", " ", question.strip().lower())


def _key(
    question: str,
    model: str,
    last_ticker: str | None = None,
    *,
    transcript_hash: str = "",
) -> str:
    """
    Cache key. `last_ticker` and `transcript_hash` fold conversational
    context into the key so a referential follow-up like "what about
    volume?" or "their names" caches separately depending on what the
    previous turns were — otherwise we'd serve the wrong cached SQL.
    PROMPT_VERSION invalidates stale entries when prompts change.
    """
    ctx = (last_ticker or "").upper()
    return f"{PROMPT_VERSION}::{model}::{ctx}::{transcript_hash}::{_normalise(question)}"


# Router decisions ----------------------------------------------------------

def lookup_route(
    question: str,
    model: str,
    last_ticker: str | None = None,
    *,
    transcript_hash: str = "",
) -> RouteCacheEntry | None:
    """Return the cached router decision for (question, model, last_ticker, transcript)."""
    return _ROUTE_CACHE.get(_key(question, model, last_ticker, transcript_hash=transcript_hash))


def store_route_template(
    question: str, model: str, template_name: str, params: dict,
    last_ticker: str | None = None,
    *,
    transcript_hash: str = "",
) -> None:
    _ROUTE_CACHE.set(
        _key(question, model, last_ticker, transcript_hash=transcript_hash),
        RouteCacheEntry(kind="template", name=template_name, params=dict(params)),
    )


def store_route_miss(
    question: str, model: str, last_ticker: str | None = None,
    *,
    transcript_hash: str = "",
) -> None:
    _ROUTE_CACHE.set(
        _key(question, model, last_ticker, transcript_hash=transcript_hash),
        RouteCacheEntry(kind="miss"),
    )


# AI-SQL fallback ----------------------------------------------------------

def lookup_ai_sql(
    question: str, model: str, last_ticker: str | None = None,
    *,
    transcript_hash: str = "",
) -> str | None:
    """Return the cached AI-generated SQL for (question, model, last_ticker, transcript)."""
    return _AI_SQL_CACHE.get(
        _key(question, model, last_ticker, transcript_hash=transcript_hash)
    )


def store_ai_sql(
    question: str, model: str, sql: str, last_ticker: str | None = None,
    *,
    transcript_hash: str = "",
) -> None:
    _AI_SQL_CACHE.set(
        _key(question, model, last_ticker, transcript_hash=transcript_hash), sql,
    )


# Maintenance --------------------------------------------------------------

def clear_all() -> None:
    """Drop every cached entry. Useful after schema changes or template edits."""
    _ROUTE_CACHE.clear()
    _AI_SQL_CACHE.clear()


def cache_stats() -> dict:
    """Return live counters for the sidebar / debug surface."""
    return {
        "route":  _ROUTE_CACHE.stats(),
        "ai_sql": _AI_SQL_CACHE.stats(),
    }

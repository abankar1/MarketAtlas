"""Tests for src/ai/cache.py — the question → AI-decision cache."""
import time

import pytest

from src.ai import cache as cache_mod
from src.ai.cache import (
    RouteCacheEntry,
    TTLCache,
    cache_stats,
    clear_all,
    lookup_ai_sql,
    lookup_route,
    store_ai_sql,
    store_route_miss,
    store_route_template,
)


# ---------------------------------------------------------------------------
# Reset module-level singletons between tests so they don't leak state
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_cache():
    clear_all()
    yield
    clear_all()


# ---------------------------------------------------------------------------
# TTLCache primitives
# ---------------------------------------------------------------------------

def test_ttl_cache_hit_then_miss_after_clear():
    c = TTLCache(max_entries=10, ttl_seconds=60)
    c.set("k", "v")
    assert c.get("k") == "v"
    c.clear()
    assert c.get("k") is None


def test_ttl_cache_expiry():
    c = TTLCache(max_entries=10, ttl_seconds=0.05)  # 50ms
    c.set("k", "v")
    assert c.get("k") == "v"
    time.sleep(0.1)
    assert c.get("k") is None


def test_ttl_cache_lru_eviction():
    c = TTLCache(max_entries=2, ttl_seconds=60)
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)  # evicts "a"
    assert c.get("a") is None
    assert c.get("b") == 2
    assert c.get("c") == 3


def test_ttl_cache_lru_promotes_on_get():
    c = TTLCache(max_entries=2, ttl_seconds=60)
    c.set("a", 1)
    c.set("b", 2)
    assert c.get("a") == 1   # promotes "a"
    c.set("c", 3)            # evicts "b" (least recently used now)
    assert c.get("a") == 1
    assert c.get("b") is None
    assert c.get("c") == 3


def test_ttl_cache_invalidate():
    c = TTLCache()
    c.set("k", "v")
    assert c.invalidate("k") is True
    assert c.get("k") is None
    assert c.invalidate("k") is False


def test_ttl_cache_stats_track_hits_misses():
    c = TTLCache()
    c.get("missing")            # miss
    c.set("k", "v")
    c.get("k")                  # hit
    c.get("k")                  # hit
    s = c.stats()
    assert s["hits"] == 2
    assert s["misses"] == 1
    assert s["size"] == 1


# ---------------------------------------------------------------------------
# Route cache — template hit
# ---------------------------------------------------------------------------

def test_route_cache_roundtrip_template():
    store_route_template(
        "Which Health Care stocks in the S&P 500 are up more than 10% in the last 30 days?",
        "claude-haiku-4-5",
        "sector_movers_with_min_return",
        {"sector": "Health Care", "index": "sp500", "days": 30, "min_return_pct": 10},
    )
    hit = lookup_route(
        "Which Health Care stocks in the S&P 500 are up more than 10% in the last 30 days?",
        "claude-haiku-4-5",
    )
    assert isinstance(hit, RouteCacheEntry)
    assert hit.kind == "template"
    assert hit.name == "sector_movers_with_min_return"
    assert hit.params == {
        "sector": "Health Care",
        "index": "sp500",
        "days": 30,
        "min_return_pct": 10,
    }


def test_route_cache_roundtrip_miss():
    store_route_miss("What's the P/E ratio for Apple?", "claude-haiku-4-5")
    hit = lookup_route("What's the P/E ratio for Apple?", "claude-haiku-4-5")
    assert isinstance(hit, RouteCacheEntry)
    assert hit.kind == "miss"
    assert hit.name is None
    assert hit.params is None


def test_route_cache_normalises_whitespace_and_case():
    store_route_template(
        "Which stocks appear in all three indices?",
        "claude-haiku-4-5",
        "cross_index_membership",
        {},
    )

    # Variants that should hit the same cache entry
    for variant in [
        "which stocks appear in all three indices?",
        "  Which Stocks Appear In All  Three Indices?  ",
        "WHICH STOCKS APPEAR IN ALL THREE INDICES?",
    ]:
        hit = lookup_route(variant, "claude-haiku-4-5")
        assert hit is not None and hit.name == "cross_index_membership", variant


def test_route_cache_keys_per_model():
    store_route_template(
        "Top 5 NASDAQ-100 stocks", "claude-haiku-4-5",
        "top_movers_by_period", {"index": "nasdaq100", "days": 7, "top_n": 5},
    )
    # Same question, different model — should miss
    assert lookup_route("Top 5 NASDAQ-100 stocks", "claude-opus-4-7") is None
    assert lookup_route("Top 5 NASDAQ-100 stocks", "claude-haiku-4-5") is not None


def test_route_cache_punctuation_distinguishes():
    """'X up 10' and 'X up 10%' route to different params; we don't strip
    punctuation to avoid false cache hits."""
    store_route_template(
        "stocks up 10",
        "claude-haiku-4-5",
        "sector_movers_with_min_return",
        {"sector": "Energy", "index": "all", "days": 30, "min_return_pct": 10},
    )
    assert lookup_route("stocks up 10", "claude-haiku-4-5") is not None
    assert lookup_route("stocks up 10%", "claude-haiku-4-5") is None


def test_route_cache_params_isolated_from_caller_mutation():
    """The cache must store an independent copy of params — mutations on the
    caller's dict afterward shouldn't pollute future hits."""
    params = {"sector": "Energy", "index": "sp500", "days": 30, "min_return_pct": 5}
    store_route_template(
        "energy gainers", "claude-haiku-4-5",
        "sector_movers_with_min_return", params,
    )
    params["sector"] = "Health Care"   # mutate caller's dict

    hit = lookup_route("energy gainers", "claude-haiku-4-5")
    assert hit.params["sector"] == "Energy", "cached params were mutated through reference"


# ---------------------------------------------------------------------------
# AI-SQL cache
# ---------------------------------------------------------------------------

def test_ai_sql_cache_roundtrip():
    sql = "SELECT MAX(adj_close) FROM daily_bars WHERE symbol = 'AAPL'"
    store_ai_sql("highest closing price for AAPL", "claude-haiku-4-5", sql)
    assert lookup_ai_sql("highest closing price for AAPL", "claude-haiku-4-5") == sql


def test_ai_sql_cache_miss():
    assert lookup_ai_sql("never asked", "claude-haiku-4-5") is None


# ---------------------------------------------------------------------------
# clear_all + stats
# ---------------------------------------------------------------------------

def test_clear_all_drops_both_caches():
    store_route_miss("q1", "claude-haiku-4-5")
    store_ai_sql("q2", "claude-haiku-4-5", "SELECT 1")
    assert cache_stats()["route"]["size"] == 1
    assert cache_stats()["ai_sql"]["size"] == 1

    clear_all()

    assert cache_stats()["route"]["size"] == 0
    assert cache_stats()["ai_sql"]["size"] == 0


def test_stats_shape():
    s = cache_stats()
    assert set(s.keys()) == {"route", "ai_sql"}
    for k in ("size", "max_entries", "ttl_seconds", "hits", "misses"):
        assert k in s["route"]
        assert k in s["ai_sql"]

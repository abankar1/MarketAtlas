"""
Database queries and session-level caching for the MarketAtlas dashboard.

Public API
----------
connect(db_url)                                     psycopg connection factory
build_universe_sql(index_key)                       CTE SQL for a given index
fetch_treemap_data(conn, index_key, date_from, date_to)
fetch_available_date_bounds(db_url, index_key)      cached 60 s
get_treemap_data_cached(db_url, index_key, ...)     session LRU cache
get_ohlcv_cached(db_url, symbol, ...)               session LRU cache
_get_session_cache()                                raw OrderedDict (for sidebar stats)
_get_ohlcv_cache()                                  raw OrderedDict (for cache clear)
"""
from __future__ import annotations

import datetime as dt
from collections import OrderedDict

import pandas as pd
import psycopg
import streamlit as st

from src.db.repositories import fetch_ohlcv


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

def connect(db_url: str) -> psycopg.Connection:
    return psycopg.connect(db_url)


# ---------------------------------------------------------------------------
# Universe SQL helpers
# ---------------------------------------------------------------------------

def build_universe_sql(index_key: str) -> str:
    """Return a SELECT CTE fragment for the chosen index universe."""
    if index_key == "sp500":
        return """
        SELECT a.symbol,
               COALESCE(a.name, a.symbol) AS name,
               COALESCE(a.gics_sector, 'Unknown') AS group_name,
               'S&P 500' AS index_name
        FROM public.assets a
        JOIN public.sp500_constituents sc
          ON sc.symbol = a.symbol AND sc.is_active IS NOT FALSE
        """
    if index_key == "nasdaq100":
        return """
        SELECT a.symbol,
               COALESCE(a.name, a.symbol) AS name,
               COALESCE(a.gics_sector, 'Unknown') AS group_name,
               'NASDAQ-100' AS index_name
        FROM public.assets a
        JOIN public.nasdaq100_constituents nc
          ON nc.symbol = a.symbol AND nc.is_active IS NOT FALSE
        """
    if index_key == "dow30":
        return """
        SELECT a.symbol,
               COALESCE(a.name, a.symbol) AS name,
               COALESCE(a.gics_sector, 'Unknown') AS group_name,
               'Dow 30' AS index_name
        FROM public.assets a
        JOIN public.dow30_constituents dc
          ON dc.symbol = a.symbol AND dc.is_active IS NOT FALSE
        """
    # all indices — deduplicated at SQL level
    return """
    SELECT DISTINCT a.symbol,
           COALESCE(a.name, a.symbol) AS name,
           COALESCE(a.gics_sector, 'Unknown') AS group_name,
           'All' AS index_name
    FROM public.assets a
    WHERE a.symbol IN (
        SELECT symbol FROM public.sp500_constituents   WHERE is_active IS NOT FALSE
        UNION SELECT symbol FROM public.nasdaq100_constituents WHERE is_active IS NOT FALSE
        UNION SELECT symbol FROM public.dow30_constituents    WHERE is_active IS NOT FALSE
    )
    """


# ---------------------------------------------------------------------------
# Core queries
# ---------------------------------------------------------------------------

def fetch_treemap_data(
    conn: psycopg.Connection,
    index_key: str,
    date_from: dt.date,
    date_to: dt.date,
) -> pd.DataFrame:
    """
    Return one row per symbol with start_close, end_close, return_pct,
    and dollar_volume for the given date range.

    Uses DISTINCT ON for O(n log n) first/last lookups within the range.
    Rows missing price data are dropped.
    """
    universe_sql = build_universe_sql(index_key)

    q = f"""
    WITH universe AS (
        {universe_sql}
    ),
    start_px AS (
        SELECT DISTINCT ON (b.symbol)
            b.symbol,
            b.close AS start_close
        FROM public.daily_bars b
        JOIN universe u ON u.symbol = b.symbol
        WHERE ((b.ts AT TIME ZONE 'UTC')::date) BETWEEN %(date_from)s AND %(date_to)s
        ORDER BY b.symbol, b.ts ASC
    ),
    end_px AS (
        SELECT DISTINCT ON (b.symbol)
            b.symbol,
            b.close AS end_close,
            b.volume AS end_volume
        FROM public.daily_bars b
        JOIN universe u ON u.symbol = b.symbol
        WHERE ((b.ts AT TIME ZONE 'UTC')::date) BETWEEN %(date_from)s AND %(date_to)s
        ORDER BY b.symbol, b.ts DESC
    )
    SELECT
        u.index_name,
        u.group_name,
        u.symbol,
        u.name,
        s.start_close,
        e.end_close,
        e.end_volume,
        CASE
            WHEN s.start_close IS NULL OR e.end_close IS NULL OR s.start_close = 0 THEN NULL
            ELSE ((e.end_close - s.start_close) / s.start_close) * 100
        END AS return_pct,
        CASE
            WHEN e.end_close IS NULL OR e.end_volume IS NULL THEN NULL
            ELSE (e.end_close * e.end_volume)
        END AS dollar_volume
    FROM universe u
    LEFT JOIN start_px s ON s.symbol = u.symbol
    LEFT JOIN end_px e ON e.symbol = u.symbol
    """

    df = pd.read_sql(q, conn, params={"date_from": date_from, "date_to": date_to})

    # Dedup cross-listed symbols in the "all" universe (keep first by index_name sort)
    if index_key == "all":
        df = df.sort_values(["symbol", "index_name"]).drop_duplicates(
            "symbol", keep="first"
        )

    return df.dropna(subset=["return_pct", "dollar_volume"])


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_index_overlap(db_url: str) -> pd.DataFrame:
    """
    Return one row per active symbol with boolean index-membership flags.

    Columns: symbol, name, sector, in_sp500 (bool), in_nasdaq100 (bool), in_dow30 (bool)

    Cached for 1 hour — constituent membership is stable between daily syncs.
    Zero new columns are needed; the three constituent tables are left-joined.
    """
    q = """
    SELECT
        a.symbol,
        COALESCE(a.name, a.symbol)         AS name,
        COALESCE(a.gics_sector, 'Unknown') AS sector,
        (sp.symbol IS NOT NULL)            AS in_sp500,
        (nd.symbol IS NOT NULL)            AS in_nasdaq100,
        (dw.symbol IS NOT NULL)            AS in_dow30
    FROM public.assets a
    LEFT JOIN public.sp500_constituents sp
        ON a.symbol = sp.symbol AND sp.is_active IS NOT FALSE
    LEFT JOIN public.nasdaq100_constituents nd
        ON a.symbol = nd.symbol AND nd.is_active IS NOT FALSE
    LEFT JOIN public.dow30_constituents dw
        ON a.symbol = dw.symbol AND dw.is_active IS NOT FALSE
    WHERE sp.symbol IS NOT NULL
       OR nd.symbol IS NOT NULL
       OR dw.symbol IS NOT NULL
    ORDER BY a.symbol
    """
    with connect(db_url) as conn:
        return pd.read_sql(q, conn)


@st.cache_data(show_spinner=False, ttl=60)
def fetch_available_date_bounds(
    db_url: str, index_key: str
) -> tuple[dt.date | None, dt.date | None]:
    """
    Return (min_date, max_date) of bars available for the selected universe.
    Result is cached for 60 seconds to keep the UI responsive.
    """
    universe_sql = build_universe_sql(index_key)

    q = f"""
    WITH universe AS (
        {universe_sql}
    )
    SELECT
        min((b.ts AT TIME ZONE 'UTC'))::date AS min_day,
        max((b.ts AT TIME ZONE 'UTC'))::date AS max_day
    FROM public.daily_bars b
    JOIN universe u ON u.symbol = b.symbol
    """

    with connect(db_url) as conn:
        df = pd.read_sql(q, conn)
    if df.empty:
        return None, None
    return df.loc[0, "min_day"], df.loc[0, "max_day"]


# ---------------------------------------------------------------------------
# Session-level LRU cache — treemap data
# ---------------------------------------------------------------------------

def _get_session_cache() -> OrderedDict:
    """Return (or create) the per-session treemap LRU cache from session_state."""
    if "treemap_cache" not in st.session_state:
        st.session_state["treemap_cache"] = OrderedDict()
    return st.session_state["treemap_cache"]


def get_treemap_data_cached(
    db_url: str,
    index_key: str,
    date_from: dt.date,
    date_to: dt.date,
    cache_size: int = 12,
) -> pd.DataFrame:
    """
    Session-level LRU cache wrapping fetch_treemap_data.

    Prevents repeated DB queries when the user toggles filters back and forth.
    Set cache_size=0 to disable caching and always hit the DB.
    """
    key = (db_url, index_key, date_from.isoformat(), date_to.isoformat())

    if "treemap_cache_hits" not in st.session_state:
        st.session_state["treemap_cache_hits"] = 0
    if "treemap_cache_misses" not in st.session_state:
        st.session_state["treemap_cache_misses"] = 0

    if cache_size <= 0:
        st.session_state["treemap_cache_misses"] += 1
        with connect(db_url) as conn:
            return fetch_treemap_data(conn, index_key, date_from, date_to)

    cache = _get_session_cache()

    if key in cache:
        st.session_state["treemap_cache_hits"] += 1
        cache.move_to_end(key)
        return cache[key]

    st.session_state["treemap_cache_misses"] += 1

    with connect(db_url) as conn:
        df = fetch_treemap_data(conn, index_key, date_from, date_to)

    cache[key] = df
    cache.move_to_end(key)
    while len(cache) > cache_size:
        cache.popitem(last=False)

    return df


# ---------------------------------------------------------------------------
# Session-level LRU cache — OHLCV data
# ---------------------------------------------------------------------------

def _get_ohlcv_cache() -> OrderedDict:
    """Return (or create) the per-session OHLCV LRU cache from session_state."""
    if "ohlcv_cache" not in st.session_state:
        st.session_state["ohlcv_cache"] = OrderedDict()
    return st.session_state["ohlcv_cache"]


def get_ohlcv_cached(
    db_url: str,
    symbol: str,
    date_from: dt.date,
    date_to: dt.date,
    cache_size: int = 20,
) -> pd.DataFrame:
    """Session-level LRU cache wrapping repositories.fetch_ohlcv."""
    key = (symbol, date_from.isoformat(), date_to.isoformat())
    cache = _get_ohlcv_cache()

    if key in cache:
        cache.move_to_end(key)
        return cache[key]

    with connect(db_url) as conn:
        df = fetch_ohlcv(conn, symbol=symbol, date_from=date_from, date_to=date_to)

    cache[key] = df
    cache.move_to_end(key)
    while len(cache) > cache_size:
        cache.popitem(last=False)

    return df

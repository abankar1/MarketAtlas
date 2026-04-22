"""
Database access layer — all SQL queries for the market data schema.

All public functions accept a psycopg.Connection and return plain Python
objects (lists, dicts, DataFrames).  No business logic lives here.

Key functions:
    upsert_asset(conn, asset_row)           Insert/update a row in public.assets
    upsert_daily_bars(conn, bars)           Bulk upsert OHLCV rows into public.daily_bars
    upsert_nasdaq_constituents(conn, rows)  Upsert NASDAQ-100 membership rows
    upsert_dow_constituents(conn, rows)     Upsert Dow 30 membership rows
    fetch_sp500_symbols(conn)               Active S&P 500 tickers
    fetch_nasdaq100_symbols(conn)           Active NASDAQ-100 tickers
    fetch_dow30_symbols(conn)               Active Dow 30 tickers
    fetch_ohlcv(conn, symbol, date_from, date_to)  OHLCV DataFrame for a symbol

Usage:
    from src.db.connection import connect
    from src.db import repositories

    with connect(db_url) as conn:
        symbols = repositories.fetch_sp500_symbols(conn)
        repositories.upsert_daily_bars(conn, bars)
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd
import psycopg


ASSET_UPSERT_SQL = """
INSERT INTO public.assets (
  symbol, name, exchange_code, exchange, asset_type, price_currency, last_refreshed, gics_sector, updated_at
)
VALUES (%(symbol)s, %(name)s, %(exchange_code)s, %(exchange)s, %(asset_type)s, %(price_currency)s, %(last_refreshed)s, %(gics_sector)s, now())
ON CONFLICT (symbol)
DO UPDATE SET
  name = EXCLUDED.name,
  exchange_code = EXCLUDED.exchange_code,
  exchange = EXCLUDED.exchange,
  asset_type = EXCLUDED.asset_type,
  price_currency = EXCLUDED.price_currency,
  last_refreshed = EXCLUDED.last_refreshed,
  gics_sector = COALESCE(EXCLUDED.gics_sector, public.assets.gics_sector),
  updated_at = now();
"""

BAR_UPSERT_SQL = """
INSERT INTO public.daily_bars (
  symbol, ts,
  open, high, low, close, volume,
  adj_open, adj_high, adj_low, adj_close, adj_volume,
  split_factor, dividend
)
VALUES (
  %(symbol)s, %(ts)s,
  %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s,
  %(adj_open)s, %(adj_high)s, %(adj_low)s, %(adj_close)s, %(adj_volume)s,
  %(split_factor)s, %(dividend)s
)
ON CONFLICT (symbol, ts)
DO UPDATE SET
  open = EXCLUDED.open,
  high = EXCLUDED.high,
  low = EXCLUDED.low,
  close = EXCLUDED.close,
  volume = EXCLUDED.volume,
  adj_open = EXCLUDED.adj_open,
  adj_high = EXCLUDED.adj_high,
  adj_low = EXCLUDED.adj_low,
  adj_close = EXCLUDED.adj_close,
  adj_volume = EXCLUDED.adj_volume,
  split_factor = EXCLUDED.split_factor,
  dividend = EXCLUDED.dividend
WHERE
  public.daily_bars.open IS DISTINCT FROM EXCLUDED.open OR
  public.daily_bars.high IS DISTINCT FROM EXCLUDED.high OR
  public.daily_bars.low IS DISTINCT FROM EXCLUDED.low OR
  public.daily_bars.close IS DISTINCT FROM EXCLUDED.close OR
  public.daily_bars.volume IS DISTINCT FROM EXCLUDED.volume OR
  public.daily_bars.adj_open IS DISTINCT FROM EXCLUDED.adj_open OR
  public.daily_bars.adj_high IS DISTINCT FROM EXCLUDED.adj_high OR
  public.daily_bars.adj_low IS DISTINCT FROM EXCLUDED.adj_low OR
  public.daily_bars.adj_close IS DISTINCT FROM EXCLUDED.adj_close OR
  public.daily_bars.adj_volume IS DISTINCT FROM EXCLUDED.adj_volume OR
  public.daily_bars.split_factor IS DISTINCT FROM EXCLUDED.split_factor OR
  public.daily_bars.dividend IS DISTINCT FROM EXCLUDED.dividend;
"""


def fetch_sp500_symbols(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol FROM public.sp500_constituents "
            "WHERE is_active IS NOT FALSE ORDER BY symbol;"
        )
        return [r[0] for r in cur.fetchall()]


def fetch_nasdaq100_symbols(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol FROM public.nasdaq100_constituents "
            "WHERE is_active IS NOT FALSE ORDER BY symbol;"
        )
        return [r[0] for r in cur.fetchall()]


def fetch_dow30_symbols(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol FROM public.dow30_constituents "
            "WHERE is_active IS NOT FALSE ORDER BY symbol;"
        )
        return [r[0] for r in cur.fetchall()]


def fetch_ohlcv(
    conn: psycopg.Connection,
    symbol: str,
    date_from: dt.date,
    date_to: dt.date,
) -> pd.DataFrame:
    q = """
    SELECT
        (ts AT TIME ZONE 'UTC')::date AS date,
        open, high, low, close, volume
    FROM public.daily_bars
    WHERE symbol = %(symbol)s
      AND (ts AT TIME ZONE 'UTC')::date BETWEEN %(date_from)s AND %(date_to)s
    ORDER BY ts ASC
    """
    return pd.read_sql(q, conn, params={"symbol": symbol, "date_from": date_from, "date_to": date_to})


def upsert_asset(conn: psycopg.Connection, asset_row: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(ASSET_UPSERT_SQL, asset_row)


def upsert_daily_bars(conn: psycopg.Connection, bars: list[dict[str, Any]]) -> None:
    if not bars:
        return
    with conn.cursor() as cur:
        cur.executemany(BAR_UPSERT_SQL, bars)


NASDAQ_UPSERT_SQL = """
INSERT INTO public.nasdaq100_constituents (
  symbol, company, icb_industry, icb_subsector, updated_at
)
VALUES (%(symbol)s, %(company)s, %(icb_industry)s, %(icb_subsector)s, now())
ON CONFLICT (symbol) DO UPDATE SET
  company = EXCLUDED.company,
  icb_industry = EXCLUDED.icb_industry,
  icb_subsector = EXCLUDED.icb_subsector,
  updated_at = now();
"""


def upsert_nasdaq_constituents(
    conn: psycopg.Connection, rows: list[dict[str, Any]]
) -> None:
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(NASDAQ_UPSERT_SQL, rows)


DOW_UPSERT_SQL = """
INSERT INTO public.dow30_constituents (
  symbol, company, exchange, industry,
  date_added, notes, index_weighting, updated_at
)
VALUES (
  %(symbol)s, %(company)s, %(exchange)s, %(industry)s,
  %(date_added)s, %(notes)s, %(index_weighting)s, now()
)
ON CONFLICT (symbol) DO UPDATE SET
  company = EXCLUDED.company,
  exchange = EXCLUDED.exchange,
  industry = EXCLUDED.industry,
  date_added = EXCLUDED.date_added,
  notes = EXCLUDED.notes,
  index_weighting = EXCLUDED.index_weighting,
  updated_at = now();
"""


def upsert_dow_constituents(conn, rows):
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(DOW_UPSERT_SQL, rows)

"""
Historical backfill — fetches up to N years of daily OHLCV bars for every
active symbol across S&P 500, NASDAQ-100, and Dow 30.

Run once on a fresh database after loading constituents.  Safe to re-run —
only fetches the date range that is missing for each symbol (incremental).

Usage:
    # Default: 10 years of history
    python -m src.backfill.backfill_10y

    # Custom window
    python -m src.backfill.backfill_10y --years 5

Reads from:
    src/config/configuration.json  (db_url, marketdata_token)

Writes to:
    public.assets       — upserts metadata row for each symbol
    public.daily_bars   — upserts OHLCV rows
"""
from __future__ import annotations
import datetime as dt
import psycopg
import argparse

from src.config.settings import load_settings
from src.marketdata.client import MarketDataClient
from src.db import repositories as db_repositories


def earliest_day(conn, symbol: str) -> dt.date | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT min((ts AT TIME ZONE 'UTC')::date)
            FROM daily_bars
            WHERE symbol = %s
            """,
            (symbol,),
        )
        row = cur.fetchone()
        return row[0] if row and row[0] else None


def latest_day(conn, symbol: str) -> dt.date | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT max((ts AT TIME ZONE 'UTC')::date)
            FROM daily_bars
            WHERE symbol = %s
            """,
            (symbol,),
        )
        row = cur.fetchone()
        return row[0] if row and row[0] else None


def daterange_chunks(start: dt.date, end: dt.date, days: int = 900):
    cur = start
    while cur <= end:
        nxt = min(end, cur + dt.timedelta(days=days - 1))
        yield cur, nxt
        cur = nxt + dt.timedelta(days=1)


def fetch_all_index_symbols(conn) -> list[str]:
    """Return symbols in priority order: NASDAQ-100, Dow 30, S&P 500 (no duplicates)."""
    seen: set[str] = set()
    result: list[str] = []
    queries = [
        "SELECT symbol FROM public.nasdaq100_constituents ORDER BY symbol",
        "SELECT symbol FROM public.dow30_constituents ORDER BY symbol",
        "SELECT symbol FROM public.sp500_constituents ORDER BY symbol",
    ]
    with conn.cursor() as cur:
        for q in queries:
            cur.execute(q)
            for (sym,) in cur.fetchall():
                if sym not in seen:
                    seen.add(sym)
                    result.append(sym)
    return result


# Helper to parse Marketstack date strings
def parse_marketstack_ts(s: str) -> dt.datetime:
    # Example: "2022-02-04T00:00:00+0000"
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")


def _fetch_and_insert(conn, client, symbol: str, date_from: dt.date, date_to: dt.date) -> int:
    inserted = 0
    for a, b in daterange_chunks(date_from, date_to):
        rows = client.fetch_daily_range(symbol, a, b)
        if not rows:
            continue
        bars = [
            {
                "symbol": symbol,
                "ts": parse_marketstack_ts(r["date"]),
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
                "volume": r["volume"],
                "adj_open": r.get("adj_open"),
                "adj_high": r.get("adj_high"),
                "adj_low": r.get("adj_low"),
                "adj_close": r.get("adj_close"),
                "adj_volume": r.get("adj_volume"),
                "split_factor": r.get("split_factor"),
                "dividend": r.get("dividend"),
            }
            for r in rows
        ]
        db_repositories.upsert_daily_bars(conn, bars)
        conn.commit()
        inserted += len(bars)
    return inserted


def backfill_symbol(conn, client, symbol: str, years: int = 10) -> int:
    today = dt.date.today()
    target_start = today - dt.timedelta(days=365 * years)

    min_day = earliest_day(conn, symbol)
    max_day = latest_day(conn, symbol)
    inserted = 0

    # Fill backward: target_start → day before earliest bar
    if not min_day or min_day > target_start:
        date_from = target_start
        date_to = (min_day - dt.timedelta(days=1)) if min_day else today
        if date_from <= date_to:
            inserted += _fetch_and_insert(conn, client, symbol, date_from, date_to)

    # Fill forward: day after latest bar → today
    if max_day and max_day < today:
        inserted += _fetch_and_insert(conn, client, symbol, max_day + dt.timedelta(days=1), today)

    return inserted


def main():
    parser = argparse.ArgumentParser(
        description="Backfill daily bars for index universes."
    )
    parser.add_argument(
        "--years",
        type=int,
        default=10,
        help="How many years of history to ensure in daily_bars (default: 10).",
    )
    args = parser.parse_args()

    settings = load_settings()
    with psycopg.connect(settings.db_url) as conn:
        # Token is stored in configuration.json as "marketdata_token"
        token = getattr(settings, "marketdata_token", None)
        if token is None and isinstance(settings, dict):
            token = settings.get("marketdata_token")

        if not token:
            raise AttributeError(
                "Missing API token in configuration. Expected 'marketdata_token' "
                "in src/config/configuration.json."
            )

        client = MarketDataClient(token)
        symbols = fetch_all_index_symbols(conn)

        for i, sym in enumerate(symbols, 1):
            try:
                n = backfill_symbol(conn, client, sym, years=args.years)
                print(f"[{i}/{len(symbols)}] {sym}: backfilled {n}")
            except Exception as e:
                conn.rollback()
                print(f"[{i}/{len(symbols)}] {sym}: ERROR {e}")


if __name__ == "__main__":
    main()

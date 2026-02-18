from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg

from src.db import repositories
from src.marketdata.client import MarketDataClient


def _parse_ts(s: str) -> dt.datetime:
    # Example: "2026-01-23T00:00:00+0000"
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")


class DailyBarImporter:
    """
    Generic daily-bar importer for any symbol set (SP500 / NASDAQ100 / DOW30 / etc).

    Incremental behavior:
      - If the symbol already has bars in `public.daily_bars`, compute how many calendar days have
        elapsed since the latest stored bar and request only that many days (plus a small buffer).
      - If no bars exist yet for the symbol, fall back to the provided `days` (default 1000).

    Note: Marketstack returns trading days only. We use calendar-day delta + buffer to avoid gaps.
    Upserts ensure re-fetching overlap days is safe.
    """

    _OVERLAP_BUFFER_DAYS = 5
    _MAX_DAYS_PER_REQUEST = 1000

    def __init__(self, conn: psycopg.Connection, client: MarketDataClient):
        self._conn = conn
        self._client = client

    def _latest_stored_bar_date(self, symbol: str) -> dt.date | None:
        # Use the existing data to determine incremental range.
        q = "SELECT max(ts)::date FROM public.daily_bars WHERE symbol = %s"
        with self._conn.cursor() as cur:
            cur.execute(q, (symbol,))
            row = cur.fetchone()
            return row[0] if row and row[0] else None

    def _days_to_fetch(self, symbol: str, fallback_days: int) -> int:
        last_date = self._latest_stored_bar_date(symbol)
        if last_date is None:
            return min(fallback_days, self._MAX_DAYS_PER_REQUEST)

        today = dt.date.today()
        delta_days = (today - last_date).days
        if delta_days <= 0:
            return 0

        # Add a small overlap buffer so we don't miss trading days (holidays/weekends/timezones).
        return min(delta_days + self._OVERLAP_BUFFER_DAYS, self._MAX_DAYS_PER_REQUEST)

    def import_symbol(self, symbol: str, days: int = 1000) -> int:
        # Only pull what we need to get current up to today.
        days_to_fetch = self._days_to_fetch(symbol, days)
        if days_to_fetch <= 0:
            return 0

        candles = self._client.fetch_daily(symbol=symbol, days=days_to_fetch)
        if not candles:
            return 0

        # Determine newest timestamp robustly (don't assume ordering).
        newest_ts = max(_parse_ts(r["date"]) for r in candles if r.get("date"))

        # assets row (metadata)
        repositories.upsert_asset(
            self._conn,
            {
                "symbol": symbol,
                "name": candles[0].get("name"),
                "exchange_code": candles[0].get("exchange_code"),
                "exchange": candles[0].get("exchange"),
                "asset_type": candles[0].get("asset_type"),
                "price_currency": candles[0].get("price_currency"),
                "last_refreshed": newest_ts.date(),
            },
        )

        # daily bars rows
        bars: list[dict[str, Any]] = []
        for r in candles:
            if not r.get("date"):
                continue
            bars.append(
                {
                    "symbol": symbol,
                    "ts": _parse_ts(r["date"]),
                    "open": r.get("open"),
                    "high": r.get("high"),
                    "low": r.get("low"),
                    "close": r.get("close"),
                    "volume": r.get("volume"),
                    "adj_open": r.get("adj_open"),
                    "adj_high": r.get("adj_high"),
                    "adj_low": r.get("adj_low"),
                    "adj_close": r.get("adj_close"),
                    "adj_volume": r.get("adj_volume"),
                    "split_factor": r.get("split_factor"),
                    "dividend": r.get("dividend"),
                }
            )

        repositories.upsert_daily_bars(self._conn, bars)

        self._conn.commit()
        return len(bars)


# Backwards-compatible name (existing imports keep working).
class Sp500DailyBarImporter(DailyBarImporter):
    pass

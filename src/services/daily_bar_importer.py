"""
Incremental daily bar importer for any symbol.

Determines how many days of data are missing for each symbol (by checking
the latest stored bar date), fetches only the gap from Marketstack, and
upserts the result into public.daily_bars.

Key class:
    DailyBarImporter(conn, client)
        .import_symbol(symbol, days=1000) -> int
            Returns the number of bar rows upserted.

Usage:
    from src.services.daily_bar_importer import DailyBarImporter
    from src.marketdata.client import MarketDataClient

    client = MarketDataClient(token=settings.marketdata_token)
    importer = DailyBarImporter(conn=conn, client=client)
    n = importer.import_symbol("AAPL", days=settings.days)
"""
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
        return self._store(symbol, candles)

    # ------------------------------------------------------------------
    # Batched top-up: up to 100 symbols per Marketstack request
    # ------------------------------------------------------------------

    _BATCH_OVERLAP_DAYS = 3

    def latest_bar_dates(self, symbols: list[str]) -> dict[str, dt.date]:
        """Latest stored bar date per symbol, in one query. Missing = no bars."""
        q = """
            SELECT symbol, max(ts)::date
            FROM public.daily_bars
            WHERE symbol = ANY(%s)
            GROUP BY symbol
        """
        with self._conn.cursor() as cur:
            cur.execute(q, (list(symbols),))
            return {sym: d for sym, d in cur.fetchall() if d is not None}

    def import_batch(self, symbols: list[str]) -> dict[str, int]:
        """
        Top up symbols that already have history using one request per 100
        symbols. Each chunk fetches from (oldest latest-bar in the chunk -
        overlap) to today; upserts make the overlap harmless. Chunks are
        built from symbols sorted by latest bar date so a lagging symbol
        doesn't widen the date range for 99 up-to-date ones.

        Returns {symbol: rows upserted} for every requested symbol
        (0 when Marketstack returned nothing for it).
        """
        latest = self.latest_bar_dates(symbols)
        ordered = sorted(symbols, key=lambda s: (latest.get(s) or dt.date.min, s))
        size = self._client.MAX_SYMBOLS_PER_REQUEST
        today = dt.date.today()
        result: dict[str, int] = {}

        for i in range(0, len(ordered), size):
            chunk = ordered[i : i + size]
            oldest = min(latest[s] for s in chunk)
            date_from = oldest - dt.timedelta(days=self._BATCH_OVERLAP_DAYS)
            rows = self._client.fetch_eod_batch(chunk, date_from, today)

            by_symbol: dict[str, list[dict[str, Any]]] = {s: [] for s in chunk}
            for r in rows:
                sym = (r.get("symbol") or "").upper()
                # Marketstack may echo class shares with a dot (BRK.B);
                # the DB stores the hyphen form (BRK-B).
                if sym not in by_symbol:
                    sym = sym.replace(".", "-")
                if sym in by_symbol:
                    by_symbol[sym].append(r)

            for sym in chunk:
                result[sym] = self._store(sym, by_symbol[sym], commit=False)
            self._conn.commit()

        return result

    def _store(
        self, symbol: str, candles: list[dict[str, Any]], *, commit: bool = True
    ) -> int:
        """Upsert asset metadata + bars for one symbol. Returns rows written."""
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
                "gics_sector": None,
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

        if commit:
            self._conn.commit()
        return len(bars)


# Backwards-compatible name (existing imports keep working).
class Sp500DailyBarImporter(DailyBarImporter):
    pass

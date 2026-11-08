"""
Marketstack API client — fetches end-of-day (EOD) OHLCV data.

Requires a Marketstack access key (free tier supported).
Configure via src/config/configuration.json as `marketdata_token`.

Key methods:
    fetch_daily(symbol, days)
        Fetch the most recent N trading days for a symbol.
        Used by DailyBarImporter for incremental daily updates.

    fetch_daily_range(symbol, date_from, date_to)
        Fetch EOD bars within an explicit date range.
        Used by the 10-year backfill script.

Usage:
    from src.marketdata.client import MarketDataClient

    client = MarketDataClient(token="your_access_key", sleep_s=0.2)
    bars = client.fetch_daily("AAPL", days=30)
    bars = client.fetch_daily_range("AAPL", date(2024, 1, 1), date(2024, 12, 31))
"""
from __future__ import annotations
from pathlib import Path

import datetime as dt
import time
import json
from typing import Any

import requests


class MarketDataClient:
    # Marketstack rejects more than 100 comma-separated symbols per request.
    MAX_SYMBOLS_PER_REQUEST = 100

    def __init__(self, token: str, timeout_s: int = 30, sleep_s: float = 0.5):
        self._token = token
        self._timeout_s = timeout_s
        self._sleep_s = sleep_s
        # Number of HTTP requests made (each one counts against the
        # Marketstack monthly quota). Reported at the end of a run.
        self.requests_made = 0

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        resp = requests.get(
            "https://api.marketstack.com/v2/eod",
            params={**params, "access_key": self._token},
            timeout=self._timeout_s,
        )
        self.requests_made += 1
        resp.raise_for_status()
        return resp.json()

    def fetch_eod_batch(
        self,
        symbols: list[str],
        date_from: dt.date,
        date_to: dt.date,
    ) -> list[dict[str, Any]]:
        """
        Fetch EOD bars for up to 100 symbols in one request (plus extra
        pages only if the result exceeds 1000 rows).

        For a daily top-up — 100 symbols x a few days — that is a single
        request, versus one request per symbol with `fetch_daily`.
        Rows carry their own `symbol` field; order is not guaranteed.
        """
        if not symbols:
            return []
        if len(symbols) > self.MAX_SYMBOLS_PER_REQUEST:
            raise ValueError(
                f"at most {self.MAX_SYMBOLS_PER_REQUEST} symbols per request, "
                f"got {len(symbols)}"
            )

        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            payload = self._get(
                {
                    "symbols": ",".join(symbols),
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                    "limit": 1000,
                    "offset": offset,
                }
            )
            data = payload.get("data") or []
            if not isinstance(data, list):
                raise ValueError("Unexpected batch response: data is not a list")
            rows.extend(data)

            pagination = payload.get("pagination") or {}
            count = int(pagination.get("count") or len(data))
            total = int(pagination.get("total") or len(data))
            if not data or count == 0 or (offset + count) >= total:
                break
            offset += count
            time.sleep(self._sleep_s)

        return rows

    def fetch_daily_range(
        self,
        symbol: str,
        date_from: dt.date,
        date_to: dt.date,
        limit: int = 1000,
        exchange: str = "NASDAQ",
    ) -> list[dict[str, Any]]:
        """
        Fetch EOD daily bars for a symbol within an explicit date range [date_from, date_to].
        Returns rows sorted oldest -> newest.

        Uses Marketstack pagination via limit/offset.
        """
        url = "https://api.marketstack.com/v2/eod"
        headers = (
            {}
        )  # Marketstack uses access_key query param, not Authorization header

        page_limit = min(limit, 1000)
        offset = 0
        rows: list[dict[str, Any]] = []

        while True:
            params = {
                "symbols": symbol,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "exchange": exchange,
                "access_key": self._token,
                "limit": page_limit,
                "offset": offset,
            }
            resp = requests.get(
                url, headers=headers, params=params, timeout=self._timeout_s
            )
            self.requests_made += 1
            resp.raise_for_status()

            payload = resp.json()
            data = payload.get("data") or []
            if not isinstance(data, list):
                raise ValueError(
                    f"Unexpected response for {symbol}: data is not a list"
                )

            if not data:
                break

            rows.extend(data)

            pagination = payload.get("pagination") or {}
            count = int(pagination.get("count") or len(data))
            total = int(pagination.get("total") or len(data))

            if count == 0 or (offset + count) >= total:
                break

            offset += count
            time.sleep(self._sleep_s)

        return sorted(rows, key=lambda r: r.get("date") or "")

    def fetch_daily(self, symbol: str, days: int = 1000) -> list[dict[str, Any]]:
        today = dt.date.today()
        start = today - dt.timedelta(days=6 * 365)

        url = "https://api.marketstack.com/v2/eod"
        headers = (
            {}
        )  # Marketstack uses access_key query param, not Authorization header

        limit = min(days, 1000)
        offset = 0
        rows: list[dict[str, Any]] = []

        while len(rows) < days:
            params = {
                "symbols": symbol,
                "date_from": start.isoformat(),
                "date_to": today.isoformat(),
                "exchange": "NASDAQ",
                "access_key": self._token,
                "limit": limit,
                "offset": offset,
            }
            resp = requests.get(
                url, headers=headers, params=params, timeout=self._timeout_s
            )
            self.requests_made += 1
            resp.raise_for_status()

            payload = resp.json()
            data = payload.get("data") or []
            if not isinstance(data, list):
                raise ValueError(
                    f"Unexpected response for {symbol}: data is not a list"
                )

            rows.extend(data)

            pagination = payload.get("pagination") or {}
            total = int(pagination.get("total") or len(data))
            count = int(pagination.get("count") or len(data))

            if count == 0 or (offset + count) >= total:
                break

            offset += count
            time.sleep(self._sleep_s)

        rows_sorted = sorted(rows, key=lambda r: r.get("date") or "", reverse=True)
        return rows_sorted[:days]

    def load_payload_from_file(self, path: str | Path) -> dict:
        p = Path(path)
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)

"""Tests for the batched Marketstack top-up (no network, no DB)."""
from __future__ import annotations

import datetime as dt

import pytest

from src.marketdata import client as client_mod
from src.marketdata.client import MarketDataClient
from src.services import daily_bar_importer as importer_mod
from src.services.daily_bar_importer import DailyBarImporter


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_eod_batch_joins_symbols_and_follows_pagination(monkeypatch):
    calls = []
    pages = [
        {"data": [{"symbol": "A"}] * 1000, "pagination": {"count": 1000, "total": 1500}},
        {"data": [{"symbol": "B"}] * 500, "pagination": {"count": 500, "total": 1500}},
    ]

    def fake_get(url, params, timeout):
        calls.append(params)
        return _Resp(pages[len(calls) - 1])

    monkeypatch.setattr(client_mod.requests, "get", fake_get)
    c = MarketDataClient(token="t", sleep_s=0)
    rows = c.fetch_eod_batch(["A", "B"], dt.date(2026, 11, 1), dt.date(2026, 11, 8))

    assert len(rows) == 1500
    assert c.requests_made == 2
    assert calls[0]["symbols"] == "A,B"
    assert calls[1]["offset"] == 1000
    assert "exchange" not in calls[0]


def test_fetch_eod_batch_rejects_more_than_100_symbols():
    c = MarketDataClient(token="t", sleep_s=0)
    with pytest.raises(ValueError):
        c.fetch_eod_batch([f"S{i}" for i in range(101)], dt.date.today(), dt.date.today())


class _FakeClient:
    MAX_SYMBOLS_PER_REQUEST = 100

    def __init__(self):
        self.batches = []

    def fetch_eod_batch(self, symbols, date_from, date_to):
        self.batches.append((list(symbols), date_from))
        # Echo class shares the way Marketstack might (dot form).
        return [
            {"symbol": s.replace("-", "."), "date": "2026-11-06T00:00:00+0000", "close": 1.0}
            for s in symbols
            if s != "SKIP"
        ]


class _FakeConn:
    commits = 0

    def commit(self):
        self.commits += 1


def test_import_batch_chunks_by_100_and_maps_symbols(monkeypatch):
    written = {}
    monkeypatch.setattr(importer_mod.repositories, "upsert_asset", lambda conn, row: None)
    monkeypatch.setattr(
        importer_mod.repositories,
        "upsert_daily_bars",
        lambda conn, bars: written.setdefault(bars[0]["symbol"], len(bars)),
    )

    symbols = [f"S{i:03d}" for i in range(248)] + ["BRK-B", "SKIP"]
    latest = {s: dt.date(2026, 11, 5) for s in symbols}
    latest["S000"] = dt.date(2026, 10, 20)  # one laggard

    fake = _FakeClient()
    imp = DailyBarImporter(conn=_FakeConn(), client=fake)
    monkeypatch.setattr(imp, "latest_bar_dates", lambda syms: latest)

    counts = imp.import_batch(symbols)

    assert [len(b) for b, _ in fake.batches] == [100, 100, 50]
    # The laggard sorts first, so only its chunk reaches back to October.
    assert fake.batches[0][1] == dt.date(2026, 10, 17)
    assert fake.batches[1][1] == dt.date(2026, 11, 2)
    assert counts["BRK-B"] == 1      # BRK.B in the response mapped back
    assert counts["SKIP"] == 0       # nothing returned -> 0, not an error
    assert sum(counts.values()) == 249

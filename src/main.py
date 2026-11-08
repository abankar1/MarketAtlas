"""
Daily update orchestrator.

Optionally syncs index constituents from Wikipedia, then fetches
the latest daily bars for all symbols across S&P 500, NASDAQ-100,
and Dow 30.

Usage:
    python -m src.main                  # sync constituents + fetch prices
    python -m src.main --skip-sync      # skip constituent sync
"""
from __future__ import annotations

import argparse
import re

from src.config.settings import load_settings
from src.db.connection import connect
from src.db.repositories import (
    fetch_sp500_symbols,
    fetch_dow30_symbols,
    fetch_nasdaq100_symbols,
)
from src.marketdata.client import MarketDataClient
from src.services.daily_bar_importer import DailyBarImporter


def _redact(message: object) -> str:
    """Strip the Marketstack key from error text (requests puts the full URL,
    query string included, into HTTPError messages)."""
    return re.sub(r"access_key=[^&\s]+", "access_key=***", str(message))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Daily update: sync constituents + fetch latest bars",
    )
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help="Skip constituent sync from Wikipedia",
    )
    args = parser.parse_args()

    settings = load_settings()

    with connect(settings.db_url) as conn:

        # --- Optional: sync index constituents ---
        if not args.skip_sync:
            try:
                from src.services.constituent_sync import ConstituentSyncService
                from src.services.sector_classifier import ensure_sectors

                print("Syncing index constituents...")
                svc = ConstituentSyncService(conn)
                svc.ensure_schema()
                results = svc.sync_all()

                all_new: list[tuple[str, str]] = []
                for r in results:
                    print(
                        f"  {r.index_name}: "
                        f"+{len(r.added)} added, "
                        f"-{len(r.removed)} removed, "
                        f"{r.unchanged} unchanged"
                    )
                    for sym in r.added:
                        all_new.append((sym, sym))

                if all_new:
                    api_key = settings.anthropic_api_key or None
                    classified = ensure_sectors(conn, all_new, api_key)
                    print(f"  Sector classification: {classified} symbol(s)")

                print()
            except Exception as e:
                print(f"WARNING: Constituent sync failed ({e}), continuing with existing data.\n")

        # --- Fetch symbols from all three indices ---
        sp = fetch_sp500_symbols(conn)
        nas = fetch_nasdaq100_symbols(conn)
        dow = fetch_dow30_symbols(conn)

        # Dedupe overlaps and keep stable order for logging
        symbols = sorted(set(sp) | set(nas) | set(dow))

        client = MarketDataClient(
            token=settings.marketdata_token, sleep_s=settings.api_sleep_seconds
        )
        importer = DailyBarImporter(conn=conn, client=client)

        failed: list[str] = []

        # Symbols with history are topped up 100-per-request; brand-new
        # symbols (no bars yet) need a long first fetch, so they still go
        # one at a time through import_symbol.
        latest = importer.latest_bar_dates(symbols)
        existing = [s for s in symbols if s in latest]
        new = [s for s in symbols if s not in latest]

        try:
            counts = importer.import_batch(existing)
            empty = sorted(s for s, n in counts.items() if n == 0)
            print(
                f"Batched top-up: {len(existing)} symbols, "
                f"{sum(counts.values())} rows upserted"
            )
            if empty:
                print(f"  No new bars returned for: {', '.join(empty)}")
        except Exception as e:
            print(f"Batched top-up: ERROR {_redact(e)}")
            failed.extend(existing)
            batch_failed = True
        else:
            batch_failed = False

        for i, sym in enumerate(new, start=1):
            try:
                n = importer.import_symbol(sym, days=settings.days)
                print(f"[new {i}/{len(new)}] {sym}: upserted {n} rows")
            except Exception as e:
                print(f"[new {i}/{len(new)}] {sym}: ERROR {_redact(e)}")
                failed.append(sym)

        print(f"Marketstack requests this run: {client.requests_made}")

        # Summary: how many symbols are current through the latest trading
        # day in the table (comparing to today is misleading on weekends).
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH latest AS (
                    SELECT symbol, max(ts)::date AS last_day
                    FROM public.daily_bars
                    WHERE symbol = ANY(%s)
                    GROUP BY symbol
                )
                SELECT
                    (SELECT max(last_day) FROM latest),
                    count(*) FILTER (WHERE last_day = (SELECT max(last_day) FROM latest)),
                    %s
                FROM latest
                """,
                (symbols, len(symbols)),
            )
            last_day, current, total = cur.fetchone()

        print(f"Summary: {current}/{total} symbols have data through {last_day}")
        if failed:
            print("Failed:", ", ".join(failed))
        # A failed batch means nothing was updated — fail the job so it
        # shows up in Actions. A single new symbol Marketstack can't serve
        # yet (e.g. a fresh ticker) shouldn't turn every run red.
        if batch_failed:
            raise SystemExit(1)


if __name__ == "__main__":
    main()

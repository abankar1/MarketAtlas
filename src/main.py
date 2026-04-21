"""
Daily update orchestrator.

Optionally syncs index constituents from yfiua GitHub Pages, then
fetches the latest daily bars for all symbols across S&P 500,
NASDAQ-100, and Dow 30.

Usage:
    python -m src.main                  # sync constituents + fetch prices
    python -m src.main --skip-sync      # skip constituent sync
"""
from __future__ import annotations

import argparse

from src.config.settings import load_settings
from src.db.connection import connect
from src.db.repositories import (
    fetch_sp500_symbols,
    fetch_dow30_symbols,
    fetch_nasdaq100_symbols,
)
from src.marketdata.client import MarketDataClient
from src.services.daily_bar_importer import DailyBarImporter


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Daily update: sync constituents + fetch latest bars",
    )
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help="Skip constituent sync from yfiua GitHub Pages",
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

        success = 0
        failed: list[str] = []

        for i, sym in enumerate(symbols, start=1):
            try:
                n = importer.import_symbol(sym, days=settings.days)
                print(f"[{i}/{len(symbols)}] {sym}: upserted {n} rows")
                success += 1
            except Exception as e:
                print(f"[{i}/{len(symbols)}] {sym}: ERROR {e}")
                failed.append(sym)

        print(f"Done. Success: {success}, Failed: {len(failed)}")

        # Summary: how many symbols are current through today
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH latest AS (
                    SELECT symbol, max(ts)::date AS last_day
                    FROM public.daily_bars
                    GROUP BY symbol
                )
                SELECT
                    count(*) FILTER (WHERE last_day = current_date) AS up_to_today,
                    count(*) AS total_symbols
                FROM latest
                """
            )
            up_to_today, total_symbols = cur.fetchone()

        print(
            f"Summary: {up_to_today}/{total_symbols} symbols "
            f"have data through today ({up_to_today * 100 // max(total_symbols, 1)}%)"
        )
        if failed:
            print("Failed:", ", ".join(failed))


if __name__ == "__main__":
    main()

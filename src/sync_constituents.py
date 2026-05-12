"""
Sync index constituents from Wikipedia.

Pulls the latest S&P 500, NASDAQ-100, and Dow 30 membership lists from
the constituent tables on each index's Wikipedia page, diffs against
the database, and handles additions/removals via soft deletes.

Usage:
    python -m src.sync_constituents                # full sync + sector classification
    python -m src.sync_constituents --dry-run      # show diff without writing
    python -m src.sync_constituents --skip-sector  # skip AI sector classification
"""
from __future__ import annotations

import argparse

from src.config.settings import load_settings
from src.db.connection import connect
from src.services.constituent_sync import ConstituentSyncService
from src.services.sector_classifier import ensure_sectors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync index constituents from Wikipedia",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing to the database",
    )
    parser.add_argument(
        "--skip-sector",
        action="store_true",
        help="Skip AI sector classification for new symbols",
    )
    args = parser.parse_args()

    settings = load_settings()

    with connect(settings.db_url) as conn:
        svc = ConstituentSyncService(conn)

        # Always run schema migration (idempotent ADD COLUMN IF NOT EXISTS)
        svc.ensure_schema()

        # Run sync (or dry run)
        if args.dry_run:
            results = svc.dry_run_all()
        else:
            results = svc.sync_all()

        # Collect all newly added symbols across indices (for sector classification)
        all_new: list[tuple[str, str]] = []
        for r in results:
            if r.added:
                # Re-fetch names from the remote data for classification
                for sym in r.added:
                    all_new.append((sym, sym))  # name will be in assets already

        # Print summary
        print("\n--- Constituent Sync Summary ---")
        for r in results:
            status = "DRY RUN" if args.dry_run else "SYNCED"
            print(
                f"  {r.index_name:<10s} [{status}]  "
                f"+{len(r.added)} added, "
                f"-{len(r.removed)} removed, "
                f"{r.unchanged} unchanged"
            )
            if r.added:
                print(f"    Added:   {', '.join(r.added)}")
            if r.removed:
                print(f"    Removed: {', '.join(r.removed)}")

        # Sector classification for new symbols
        if all_new and not args.dry_run and not args.skip_sector:
            api_key = settings.anthropic_api_key or None
            classified = ensure_sectors(conn, all_new, api_key)
            print(f"\n  Sector classification: {classified} symbol(s) classified.")
        elif all_new and args.dry_run:
            print(f"\n  {len(all_new)} new symbol(s) would need sector classification.")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

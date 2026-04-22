"""
Load GICS sector classifications from a static JSON file into the assets table.

The file data/gics_sectors.json is a flat {symbol: sector} mapping that serves
as the single source of truth for sector classification.  Edit it manually
when new stocks join an index, then re-run this script to push changes to
the database.

Usage:
    python -m src.load_sectors                # apply sectors from file
    python -m src.load_sectors --check        # show unclassified symbols without writing
    python -m src.load_sectors --export       # dump current DB sectors to the file
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import psycopg

from src.config.settings import load_settings
from src.db.connection import connect
from src.services.sector_classifier import GICS_SECTORS

SECTORS_FILE = Path(__file__).resolve().parent.parent / "data" / "gics_sectors.json"


def load_sector_map() -> dict[str, str]:
    """Read the static sector mapping from disk."""
    with open(SECTORS_FILE, "r") as f:
        return json.load(f)


def save_sector_map(mapping: dict[str, str]) -> None:
    """Write the sector mapping to disk (sorted)."""
    with open(SECTORS_FILE, "w") as f:
        json.dump(dict(sorted(mapping.items())), f, indent=2)
        f.write("\n")


def apply_sectors(conn: psycopg.Connection, mapping: dict[str, str]) -> int:
    """Update assets.gics_sector from the mapping. Returns count of rows updated."""
    count = 0
    with conn.cursor() as cur:
        for symbol, sector in mapping.items():
            if sector not in GICS_SECTORS:
                print(f"  WARNING: '{sector}' is not a valid GICS sector for {symbol} — skipping")
                continue
            cur.execute(
                "UPDATE public.assets SET gics_sector = %s "
                "WHERE symbol = %s AND (gics_sector IS NULL OR gics_sector != %s);",
                (sector, symbol, sector),
            )
            if cur.rowcount:
                count += cur.rowcount
    conn.commit()
    return count


def check_unclassified(conn: psycopg.Connection, mapping: dict[str, str]) -> list[tuple[str, str]]:
    """Find symbols in the DB that have no sector and no entry in the mapping."""
    rows = conn.execute(
        "SELECT symbol, COALESCE(name, symbol) FROM public.assets "
        "WHERE gics_sector IS NULL ORDER BY symbol;"
    ).fetchall()

    missing = []
    for sym, name in rows:
        if sym not in mapping:
            missing.append((sym, name))
    return missing


def export_sectors(conn: psycopg.Connection) -> dict[str, str]:
    """Dump current DB sectors to the mapping file."""
    rows = conn.execute(
        "SELECT symbol, gics_sector FROM public.assets "
        "WHERE gics_sector IS NOT NULL ORDER BY symbol;"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load GICS sector classifications from data/gics_sectors.json",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Show unclassified symbols without writing to DB",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Export current DB sectors to data/gics_sectors.json",
    )
    args = parser.parse_args()

    settings = load_settings()

    with connect(settings.db_url) as conn:

        if args.export:
            mapping = export_sectors(conn)
            save_sector_map(mapping)
            print(f"Exported {len(mapping)} sector mappings to {SECTORS_FILE}")
            return 0

        # Load the static file
        if not SECTORS_FILE.exists():
            print(f"ERROR: {SECTORS_FILE} not found.")
            print("Run with --export first to generate it from the database.")
            return 1

        mapping = load_sector_map()
        print(f"Loaded {len(mapping)} sector mappings from {SECTORS_FILE.name}")

        if args.check:
            missing = check_unclassified(conn, mapping)
            if missing:
                print(f"\n{len(missing)} symbol(s) have no sector in DB or file:")
                for sym, name in missing:
                    print(f"  {sym}: {name}")
                print(f"\nAdd them to {SECTORS_FILE.name} and re-run without --check.")
            else:
                in_file = check_unclassified(conn, {})
                covered = len(in_file) - len(missing)
                print(f"\nAll unclassified DB symbols have entries in the file.")
                if in_file:
                    print(f"Run without --check to apply {len(in_file)} pending sector(s).")
            return 0

        # Apply sectors
        updated = apply_sectors(conn, mapping)
        print(f"Updated {updated} symbol(s) in the database.")

        # Report any still missing
        missing = check_unclassified(conn, mapping)
        if missing:
            print(f"\n{len(missing)} symbol(s) still need sectors added to {SECTORS_FILE.name}:")
            for sym, name in missing:
                print(f"  {sym}: {name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

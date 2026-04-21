"""
Migration script: Unify stock sector classification.

Adds a gics_sector column to the assets table and populates it in three tiers:
  1. Backfill from sp500_constituents (accurate GICS data for ~500 symbols)
  2. Map ICB → GICS for remaining NASDAQ-100 symbols
  3. Use Anthropic Claude API to classify any remaining unmapped symbols

Usage:
    python -m scripts.migrate_add_sector
    python -m scripts.migrate_add_sector --skip-ai   # skip step 3
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import psycopg

from src.services.sector_classifier import ICB_TO_GICS, classify_via_api

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_FILE = Path(__file__).resolve().parent.parent / "src" / "config" / "configuration.json"


def load_config() -> dict:
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Step 1 — Add column
# ---------------------------------------------------------------------------

def step1_add_column(conn: psycopg.Connection) -> None:
    print("Step 1: Adding gics_sector column to assets table...")
    conn.execute("ALTER TABLE public.assets ADD COLUMN IF NOT EXISTS gics_sector TEXT;")
    conn.commit()
    print("  → Done (column exists).")


# ---------------------------------------------------------------------------
# Step 2 — Backfill from S&P 500
# ---------------------------------------------------------------------------

def step2_backfill_sp500(conn: psycopg.Connection) -> int:
    print("Step 2: Backfilling from sp500_constituents...")
    cur = conn.execute("""
        UPDATE public.assets a
        SET gics_sector = sc.gics_sector
        FROM public.sp500_constituents sc
        WHERE a.symbol = sc.symbol
          AND sc.gics_sector IS NOT NULL
          AND sc.gics_sector != ''
          AND a.gics_sector IS NULL;
    """)
    count = cur.rowcount
    conn.commit()
    print(f"  → Updated {count} symbols from S&P 500 GICS data.")
    return count


# ---------------------------------------------------------------------------
# Step 3 — ICB → GICS mapping for NASDAQ-100 remainders
# ---------------------------------------------------------------------------

def step3_map_icb(conn: psycopg.Connection) -> int:
    print("Step 3: Mapping ICB → GICS for remaining NASDAQ-100 symbols...")
    # Build CASE expression from mapping
    cases = "\n".join(
        f"        WHEN n.icb_industry = '{icb}' THEN '{gics}'"
        for icb, gics in ICB_TO_GICS.items()
    )
    sql = f"""
        UPDATE public.assets a
        SET gics_sector = CASE
{cases}
            ELSE n.icb_industry
        END
        FROM public.nasdaq100_constituents n
        WHERE a.symbol = n.symbol
          AND a.gics_sector IS NULL
          AND n.icb_industry IS NOT NULL;
    """
    cur = conn.execute(sql)
    count = cur.rowcount
    conn.commit()
    print(f"  → Updated {count} symbols via ICB → GICS mapping.")
    return count


# ---------------------------------------------------------------------------
# Step 4 — AI classification for remaining unmapped symbols
# ---------------------------------------------------------------------------

def step4_ai_classify(conn: psycopg.Connection, api_key: str) -> int:
    print("Step 4: AI classification for remaining unmapped symbols...")

    # Find unmapped symbols
    rows = conn.execute("""
        SELECT symbol, COALESCE(name, symbol) AS name
        FROM public.assets
        WHERE gics_sector IS NULL
        ORDER BY symbol;
    """).fetchall()

    if not rows:
        print("  → No unmapped symbols. Nothing to do.")
        return 0

    symbols = [(r[0], r[1]) for r in rows]
    print(f"  → Found {len(symbols)} unmapped symbols: {[s[0] for s in symbols]}")

    try:
        classifications = classify_via_api(symbols, api_key)
    except ImportError:
        print("  ⚠ anthropic package not installed. Run: pip install anthropic")
        print("  → Skipping AI classification.")
        return 0
    except Exception as e:
        print(f"  ⚠ AI classification failed: {e}")
        return 0

    count = 0
    for sym, sector in classifications.items():
        conn.execute(
            "UPDATE public.assets SET gics_sector = %s WHERE symbol = %s AND gics_sector IS NULL;",
            (sector, sym),
        )
        print(f"  ✓ {sym} → {sector}")
        count += 1

    conn.commit()
    print(f"  → AI classified {count} symbols.")
    return count


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(conn: psycopg.Connection) -> None:
    print("\n--- Sector Distribution ---")
    rows = conn.execute("""
        SELECT COALESCE(gics_sector, '(unmapped)') AS sector, COUNT(*)
        FROM public.assets
        GROUP BY 1
        ORDER BY 2 DESC;
    """).fetchall()
    for sector, cnt in rows:
        print(f"  {sector:<30s} {cnt:>4d}")

    unmapped = conn.execute(
        "SELECT COUNT(*) FROM public.assets WHERE gics_sector IS NULL;"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM public.assets;").fetchone()[0]
    print(f"\nTotal: {total} symbols, {total - unmapped} mapped, {unmapped} unmapped.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate assets table: add unified GICS sector")
    parser.add_argument("--skip-ai", action="store_true", help="Skip AI classification step")
    args = parser.parse_args()

    cfg = load_config()
    db_url = cfg["db_url"]
    api_key = cfg.get("anthropic_api_key")

    with psycopg.connect(db_url) as conn:
        step1_add_column(conn)
        step2_backfill_sp500(conn)
        step3_map_icb(conn)

        if args.skip_ai:
            print("Step 4: Skipped (--skip-ai flag).")
        elif not api_key:
            print("Step 4: Skipped (no anthropic_api_key in configuration.json).")
        else:
            step4_ai_classify(conn, api_key)

        print_summary(conn)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

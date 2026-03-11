from __future__ import annotations

import argparse
import datetime as dt
import subprocess
from pathlib import Path

import psycopg


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "src" / "config" / "configuration.json"


def load_db_url_from_repo_config() -> str:
    import json

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config file: {CONFIG_PATH}")

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    db_url = cfg.get("db_url")
    if not db_url:
        raise ValueError(f"Missing 'db_url' in {CONFIG_PATH}")

    return db_url


def run_snapshot_tables(conn: psycopg.Connection, stamp: str) -> None:
    """
    Creates snapshot tables in schema backup with timestamp suffix, e.g.
      backup.assets_2026_01_25_1352
      backup.daily_bars_2026_01_25_1352
      backup.sp500_constituents_2026_01_25_1352
    """
    schema = "backup"
    assets_bak = f"assets_{stamp}"
    bars_bak = f"daily_bars_{stamp}"
    sp500_bak = f"sp500_constituents_{stamp}"

    sql = f"""
    CREATE SCHEMA IF NOT EXISTS {schema};

    CREATE TABLE {schema}.{assets_bak} AS
      SELECT * FROM public.assets;

    CREATE TABLE {schema}.{bars_bak} AS
      SELECT * FROM public.daily_bars;

    CREATE TABLE {schema}.{sp500_bak} AS
      SELECT * FROM public.sp500_constituents;

    -- Recreate useful constraints on the backup copies
    ALTER TABLE {schema}.{assets_bak} ADD PRIMARY KEY (symbol);
    ALTER TABLE {schema}.{bars_bak} ADD PRIMARY KEY (symbol, ts);
    ALTER TABLE {schema}.{sp500_bak} ADD PRIMARY KEY (symbol);
    """

    with conn.cursor() as cur:
        cur.execute(sql)

    conn.commit()

    print("✅ Created snapshot tables:")
    print(f"  - backup.{assets_bak}")
    print(f"  - backup.{bars_bak}")
    print(f"  - backup.{sp500_bak}")


def run_pg_dump(db_url: str, out_file: Path) -> None:
    """
    Runs a custom-format pg_dump backup.
    Uses pg_dump from your PATH.
    """
    out_file.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["pg_dump", db_url, "-F", "c", "-f", str(out_file)]
    print("🔧 Running:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f"✅ Wrote pg_dump backup: {out_file}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dump", action="store_true", help="Also create a pg_dump file backup"
    )
    ap.add_argument(
        "--dump-dir",
        default=str(PROJECT_ROOT / "backups"),
        help="Directory for pg_dump output (default: <repo>/backups)",
    )
    ap.add_argument(
        "--stamp",
        default=None,
        help="Timestamp suffix. Default: now in YYYY_MM_DD_HHMM",
    )
    args = ap.parse_args()

    stamp = args.stamp or dt.datetime.now().strftime("%Y_%m_%d_%H%M")

    db_url = load_db_url_from_repo_config()

    # 1) Snapshot tables
    with psycopg.connect(db_url) as conn:
        run_snapshot_tables(conn, stamp)

    # 2) Optional pg_dump
    if args.dump:
        dump_dir = Path(args.dump_dir)
        dump_file = dump_dir / f"market_timeseries_{stamp}.dump"
        run_pg_dump(db_url, dump_file)


if __name__ == "__main__":
    main()

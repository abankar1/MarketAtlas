import os
import csv
import sys
from typing import Optional

import psycopg


UPSERT_SQL = """
INSERT INTO assets (symbol, name, exchange_code, exchange_mic, asset_type, price_currency)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol) DO UPDATE SET
  name = EXCLUDED.name,
  exchange_code = EXCLUDED.exchange_code,
  exchange_mic = EXCLUDED.exchange_mic,
  asset_type = EXCLUDED.asset_type,
  price_currency = EXCLUDED.price_currency,
  updated_at = now()
RETURNING asset_id;
"""


def to_int(v: str) -> Optional[int]:
    v = (v or "").strip()
    return int(v) if v else None


def to_bigint(v: str) -> Optional[int]:
    v = (v or "").strip()
    return int(v) if v else None


def to_date(v: str) -> Optional[str]:
    # Keep as YYYY-MM-DD string; Postgres will cast it to date automatically
    v = (v or "").strip()
    return v if v else None


def normalize_row(row: dict) -> tuple:
    # Expected headers:
    # Symbol,Security,GICS Sector,GICS Sub-Industry,Headquarters Location,Date added,CIK,Founded
    return (
        (row.get("Symbol") or "").strip(),
        (row.get("Security") or "").strip() or None,
        (row.get("GICS Sector") or "").strip() or None,
        (row.get("GICS Sub-Industry") or "").strip() or None,
        (row.get("Headquarters Location") or "").strip() or None,
        to_date(row.get("Date added", "")),
        to_bigint(row.get("CIK", "")),
        (row.get("Founded") or "").strip() or None,
    )


def main() -> int:
    db_url = os.getenv("DATABASE_URL", "postgresql://localhost:5432/stocks")
    csv_file = os.getenv("CSV_FILE", "snp500.csv")

    if not os.path.exists(csv_file):
        print(f"ERROR: CSV file not found: {csv_file}", file=sys.stderr)
        print(
            "Tip: place your file next to this script or set CSV_FILE=/path/to/file.csv",
            file=sys.stderr,
        )
        return 2

    rows = []
    with open(csv_file, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        missing = [
            h for h in ["Symbol", "Security"] if h not in (reader.fieldnames or [])
        ]
        if missing:
            print(f"ERROR: CSV missing expected headers: {missing}", file=sys.stderr)
            print(f"Found headers: {reader.fieldnames}", file=sys.stderr)
            return 3

        for r in reader:
            tup = normalize_row(r)
            if not tup[0]:
                continue  # skip empty symbol rows
            rows.append(tup)

    if not rows:
        print("No rows to insert.")
        return 0

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.executemany(UPSERT_SQL, rows)
        conn.commit()

    print(f"Upserted {len(rows)} rows into stocks.sp500_constituents from {csv_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

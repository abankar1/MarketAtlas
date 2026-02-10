from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg

from src.db import repositories


class NasdaqConstituentsLoader:
    """
    Loads NASDAQ-100 constituents from a TSV/CSV-like file with headers:
      Ticker, Company, ICB Industry[14], ICB Subsector[14]

    Your sample looks TAB-separated, so we default to TSV.
    """

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def load_from_tsv(self, path: str) -> int:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Nasdaq input file not found: {p}")

        raw = p.read_text(encoding="utf-8").splitlines()
        if not raw:
            return 0

        header = raw[0].split("\t")
        # Expected header names from your sample
        # Ticker, Company, ICB Industry[14], ICB Subsector[14]
        idx = {name: i for i, name in enumerate(header)}

        def get(row_parts: list[str], key: str) -> str:
            i = idx.get(key)
            if i is None or i >= len(row_parts):
                return ""
            return (row_parts[i] or "").strip()

        rows: list[dict[str, Any]] = []
        for line in raw[1:]:
            if not line.strip():
                continue
            parts = line.split("\t")

            symbol = get(parts, "Ticker")
            if not symbol:
                continue

            rows.append(
                {
                    "symbol": symbol,
                    "company": get(parts, "Company") or None,
                    "icb_industry": get(parts, "ICB Industry[14]") or None,
                    "icb_subsector": get(parts, "ICB Subsector[14]") or None,
                }
            )

        repositories.upsert_nasdaq_constituents(self._conn, rows)
        self._conn.commit()
        return len(rows)

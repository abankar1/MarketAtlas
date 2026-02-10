from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg

from src.db import repositories


class DowConstituentsLoader:
    """
    Loads Dow 30 constituents from a TSV file that typically starts with a descriptive title line, e.g.:

      DJIA component companies, showing trading exchange, ticker symbols and industry
      Company\tExchange\tSymbol\tIndustry\tDate added\tNotes\tIndex weighting
      3M\tNYSE\tMMM\tConglomerate\t1976-08-09\tAs ...\t2.17%
      ...

    So we:
      - find the first line that looks like the header (must contain 'Company' and 'Symbol')
      - parse all following rows as tab-separated values
    """

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def load_from_tsv(self, path: str) -> int:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Dow input file not found: {p}")

        lines = p.read_text(encoding="utf-8").splitlines()
        if not lines:
            return 0

        # Find the header row (some sources include a descriptive line first)
        header_idx = None
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            cols = [c.strip() for c in line.split("\t")]
            if "Company" in cols and "Symbol" in cols:
                header_idx = i
                header = cols
                break

        if header_idx is None:
            sample = "\n".join(lines[:5])
            raise ValueError(
                "Dow loader: could not find header row containing 'Company' and 'Symbol'. "
                "Expected a tab-separated header.\n"
                f"First lines:\n{sample}"
            )

        idx = {name: j for j, name in enumerate(header)}

        def get(parts: list[str], key: str) -> str:
            j = idx.get(key)
            if j is None or j >= len(parts):
                return ""
            return (parts[j] or "").strip()

        rows: list[dict[str, Any]] = []
        for line in lines[header_idx + 1 :]:
            if not line.strip():
                continue

            parts = [c.strip() for c in line.split("\t")]
            symbol = get(parts, "Symbol")
            if not symbol:
                continue

            weight = get(parts, "Index weighting").replace("%", "").strip()

            rows.append(
                {
                    "symbol": symbol,
                    "company": get(parts, "Company") or None,
                    "exchange": get(parts, "Exchange") or None,
                    "industry": get(parts, "Industry") or None,
                    # Store as string; Postgres will cast to date when inserting into date column
                    "date_added": get(parts, "Date added") or None,
                    "notes": get(parts, "Notes") or None,
                    "index_weighting": float(weight) if weight else None,
                }
            )

        # Deterministic processing for logs/debugging
        rows.sort(key=lambda r: r["symbol"])

        repositories.upsert_dow_constituents(self._conn, rows)
        self._conn.commit()
        return len(rows)

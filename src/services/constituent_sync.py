"""
Constituent sync service — pulls latest index membership from
yfiua/index-constituents (GitHub Pages) and diffs against the database.

Handles additions (new stocks joining an index) and removals (stocks
dropped from an index) via soft deletes.

Usage:
    from src.services.constituent_sync import ConstituentSyncService

    with psycopg.connect(db_url) as conn:
        svc = ConstituentSyncService(conn)
        svc.ensure_schema()
        results = svc.sync_all()
"""
from __future__ import annotations

from dataclasses import dataclass, field

import requests
import psycopg


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://yfiua.github.io/index-constituents"

YFIUA_URLS: dict[str, str] = {
    "sp500":    f"{BASE_URL}/constituents-sp500.json",
    "nasdaq100": f"{BASE_URL}/constituents-nasdaq100.json",
    "dow30":    f"{BASE_URL}/constituents-dowjones.json",
}

INDEX_TABLES: dict[str, str] = {
    "sp500":    "sp500_constituents",
    "nasdaq100": "nasdaq100_constituents",
    "dow30":    "dow30_constituents",
}

# The "company name" column differs per table
COMPANY_COL: dict[str, str] = {
    "sp500":    "security",
    "nasdaq100": "company",
    "dow30":    "company",
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class SyncResult:
    index_name: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class ConstituentSyncService:
    """Syncs index membership from yfiua GitHub Pages JSON endpoints."""

    def __init__(self, conn: psycopg.Connection, timeout_s: int = 30) -> None:
        self._conn = conn
        self._timeout_s = timeout_s

    # -- Schema migration (idempotent) ------------------------------------

    def ensure_schema(self) -> None:
        """Add is_active, removed_date, updated_at columns if missing."""
        stmts = [
            # sp500 — needs all three columns (no updated_at previously)
            """ALTER TABLE public.sp500_constituents
               ADD COLUMN IF NOT EXISTS is_active    BOOLEAN     DEFAULT TRUE,
               ADD COLUMN IF NOT EXISTS removed_date DATE,
               ADD COLUMN IF NOT EXISTS updated_at   TIMESTAMPTZ DEFAULT now();""",
            # nasdaq100 — already has updated_at
            """ALTER TABLE public.nasdaq100_constituents
               ADD COLUMN IF NOT EXISTS is_active    BOOLEAN DEFAULT TRUE,
               ADD COLUMN IF NOT EXISTS removed_date DATE;""",
            # dow30 — already has updated_at
            """ALTER TABLE public.dow30_constituents
               ADD COLUMN IF NOT EXISTS is_active    BOOLEAN DEFAULT TRUE,
               ADD COLUMN IF NOT EXISTS removed_date DATE;""",
        ]
        for sql in stmts:
            self._conn.execute(sql)
        self._conn.commit()

    # -- Remote fetch ------------------------------------------------------

    def fetch_remote(self, index_name: str) -> list[tuple[str, str]]:
        """
        Fetch the current constituent list from yfiua GitHub Pages.

        Returns a list of (symbol, name) tuples.
        """
        url = YFIUA_URLS[index_name]
        resp = requests.get(url, timeout=self._timeout_s)
        resp.raise_for_status()

        data: list[dict[str, str]] = resp.json()

        results: list[tuple[str, str]] = []
        for entry in data:
            symbol = (entry.get("Symbol") or "").strip()
            name = (entry.get("Name") or "").strip()
            if symbol:
                results.append((symbol, name))

        return results

    # -- DB queries --------------------------------------------------------

    def fetch_current_active(self, index_name: str) -> set[str]:
        """Return the set of currently active symbols in this index."""
        table = INDEX_TABLES[index_name]
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT symbol FROM public.{table} "
                f"WHERE is_active IS NOT FALSE;"
            )
            return {r[0] for r in cur.fetchall()}

    # -- Upsert / soft-delete ----------------------------------------------

    def upsert_additions(
        self,
        index_name: str,
        additions: list[tuple[str, str]],
    ) -> int:
        """
        Insert new symbols or reactivate previously removed ones.
        Also ensures a minimal row exists in the assets table.
        """
        if not additions:
            return 0

        table = INDEX_TABLES[index_name]
        col = COMPANY_COL[index_name]

        with self._conn.cursor() as cur:
            for symbol, name in additions:
                # Upsert into the constituent table
                cur.execute(
                    f"INSERT INTO public.{table} (symbol, {col}, is_active, updated_at) "
                    f"VALUES (%s, %s, TRUE, now()) "
                    f"ON CONFLICT (symbol) DO UPDATE SET "
                    f"  {col} = COALESCE(EXCLUDED.{col}, public.{table}.{col}), "
                    f"  is_active = TRUE, "
                    f"  removed_date = NULL, "
                    f"  updated_at = now();",
                    (symbol, name),
                )

                # Ensure a minimal assets row exists (for sector classification)
                cur.execute(
                    "INSERT INTO public.assets (symbol, name) "
                    "VALUES (%s, %s) "
                    "ON CONFLICT (symbol) DO NOTHING;",
                    (symbol, name),
                )

        return len(additions)

    def soft_delete_removals(
        self,
        index_name: str,
        removals: set[str],
    ) -> int:
        """Mark removed symbols as inactive with today's date."""
        if not removals:
            return 0

        table = INDEX_TABLES[index_name]
        removal_list = list(removals)

        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE public.{table} "
                f"SET is_active = FALSE, "
                f"    removed_date = CURRENT_DATE, "
                f"    updated_at = now() "
                f"WHERE symbol = ANY(%s) AND is_active IS NOT FALSE;",
                (removal_list,),
            )
            return cur.rowcount

    # -- Orchestration -----------------------------------------------------

    def sync_index(self, index_name: str) -> SyncResult:
        """Full sync for one index: fetch, diff, upsert, soft-delete."""
        remote = self.fetch_remote(index_name)
        remote_set = {s for s, _ in remote}
        remote_map = {s: n for s, n in remote}

        current_active = self.fetch_current_active(index_name)

        additions_set = remote_set - current_active
        removals_set = current_active - remote_set

        additions = [(s, remote_map[s]) for s in sorted(additions_set)]
        self.upsert_additions(index_name, additions)
        self.soft_delete_removals(index_name, removals_set)

        self._conn.commit()

        return SyncResult(
            index_name=index_name,
            added=[s for s, _ in additions],
            removed=sorted(removals_set),
            unchanged=len(current_active & remote_set),
        )

    def sync_all(self) -> list[SyncResult]:
        """Sync all three indices. Returns list of SyncResult."""
        results: list[SyncResult] = []
        for index_name in YFIUA_URLS:
            result = self.sync_index(index_name)
            results.append(result)
        return results

    # -- Dry run -----------------------------------------------------------

    def dry_run_index(self, index_name: str) -> SyncResult:
        """Show what would change without writing to DB."""
        remote = self.fetch_remote(index_name)
        remote_set = {s for s, _ in remote}

        current_active = self.fetch_current_active(index_name)

        additions_set = remote_set - current_active
        removals_set = current_active - remote_set

        return SyncResult(
            index_name=index_name,
            added=sorted(additions_set),
            removed=sorted(removals_set),
            unchanged=len(current_active & remote_set),
        )

    def dry_run_all(self) -> list[SyncResult]:
        """Dry run all three indices."""
        return [self.dry_run_index(idx) for idx in YFIUA_URLS]

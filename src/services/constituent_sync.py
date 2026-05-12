"""
Constituent sync service — pulls latest index membership from
Wikipedia and diffs against the database.

Handles additions (new stocks joining an index) and removals (stocks
dropped from an index) via soft deletes.

We previously used yfiua/index-constituents on GitHub Pages but that
data lagged real index changes by weeks (e.g. CTRA stayed listed long
after S&P removed it). Wikipedia is community-maintained, typically
same-day for additions/deletions, and exposes the constituent table
with a known anchor (id="constituents") on each index page.

Usage:
    from src.services.constituent_sync import ConstituentSyncService

    with psycopg.connect(db_url) as conn:
        svc = ConstituentSyncService(conn)
        svc.ensure_schema()
        results = svc.sync_all()
"""
from __future__ import annotations

from dataclasses import dataclass, field
from io import StringIO

import pandas as pd
import requests
import psycopg


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WIKIPEDIA_URLS: dict[str, str] = {
    "sp500":    "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "nasdaq100": "https://en.wikipedia.org/wiki/Nasdaq-100",
    "dow30":    "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average",
}

# Browser-style UA so Wikipedia doesn't 403 us as a generic bot.
_USER_AGENT = (
    "MarketAtlas/1.0 (https://github.com/abankar1/MarketAtlas; "
    "constituent-sync) python-requests"
)

# Each index page exposes its constituents as the table with
# id="constituents". Column names vary across pages — these are the
# accepted aliases we look up by case-insensitive match.
_SYMBOL_COL_ALIASES = ("symbol", "ticker")
_NAME_COL_ALIASES = ("security", "company", "name")

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
    """Syncs index membership from Wikipedia constituent tables."""

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
        Fetch the current constituent list from Wikipedia.

        Reads the table with id="constituents" on the index page and
        extracts (symbol, name) pairs. Symbol/name column names differ
        across pages (Symbol vs Ticker, Security vs Company), so we
        resolve them by matching against known aliases.

        Class-share symbols are normalized from Wikipedia's dot form
        (BRK.B, BF.B) to the hyphen form (BRK-B, BF-B) used by Yahoo
        and Marketstack — the same form we already store in the DB.

        Returns a list of (symbol, name) tuples.
        """
        url = WIKIPEDIA_URLS[index_name]
        resp = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=self._timeout_s,
        )
        resp.raise_for_status()

        # `attrs={"id": "constituents"}` filters to just the constituent
        # table; pandas raises if no match, so a structural change on
        # Wikipedia surfaces as a clear failure rather than silent
        # parsing of the wrong table.
        tables = pd.read_html(StringIO(resp.text), attrs={"id": "constituents"})
        if not tables:
            raise RuntimeError(
                f"Wikipedia page for '{index_name}' has no table with "
                f"id='constituents' (page structure may have changed): {url}"
            )
        df = tables[0]

        # Resolve symbol + name columns by case-insensitive alias match.
        cols_lower = {c.lower(): c for c in df.columns}
        symbol_col = next(
            (cols_lower[a] for a in _SYMBOL_COL_ALIASES if a in cols_lower),
            None,
        )
        name_col = next(
            (cols_lower[a] for a in _NAME_COL_ALIASES if a in cols_lower),
            None,
        )
        if symbol_col is None or name_col is None:
            raise RuntimeError(
                f"Wikipedia '{index_name}' table is missing expected "
                f"columns. Got {list(df.columns)}; need one of "
                f"{_SYMBOL_COL_ALIASES} for symbol and one of "
                f"{_NAME_COL_ALIASES} for name."
            )

        results: list[tuple[str, str]] = []
        for raw_symbol, raw_name in zip(df[symbol_col], df[name_col]):
            symbol = str(raw_symbol).strip().upper().replace(".", "-")
            name = str(raw_name).strip()
            if symbol and symbol.lower() != "nan":
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
        for index_name in WIKIPEDIA_URLS:
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
        return [self.dry_run_index(idx) for idx in WIKIPEDIA_URLS]

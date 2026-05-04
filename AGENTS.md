# MarketAtlas — Agent Context

This file describes the MarketAtlas codebase for AI agents. It covers architecture,
data flow, every significant file and function, database schema, CLI contracts,
caching, and known behavioral constraints.

---

## Project Identity

**Type:** Streamlit dashboard + Python data pipeline  
**Purpose:** Track and visualize price performance of S&P 500, NASDAQ-100, and Dow 30 constituents  
**Stack:** Python 3.14, Streamlit, TimescaleDB (PostgreSQL), psycopg3, Plotly, Pandas, Marketstack API  
**Working directory:** repo root (all `python -m` commands run from here)

---

## Repository Layout

```
repo root/
├── src/
│   ├── config/
│   │   ├── configuration.json          # real credentials — git-ignored
│   │   ├── configuration.json.example  # committed placeholder
│   │   └── settings.py                 # loads config, exposes Settings dataclass
│   ├── db/
│   │   ├── connection.py               # psycopg.connect() wrapper
│   │   └── repositories.py             # all SQL — upserts, fetches, OHLCV query
│   ├── marketdata/
│   │   └── client.py                   # Marketstack REST client
│   ├── services/
│   │   ├── constituent_sync.py         # live sync against yfiua GitHub Pages
│   │   ├── daily_bar_importer.py       # incremental OHLCV importer
│   │   └── sector_classifier.py        # GICS sector logic + Claude API fallback
│   ├── backfill/
│   │   └── backfill_10y.py             # one-time historical backfill
│   ├── dashboard/
│   │   ├── app.py                      # Streamlit entry point — config, sidebar, nav
│   │   ├── data.py                     # DB queries + session-level LRU caches
│   │   ├── charts.py                   # Plotly figure builders (treemap + candlestick)
│   │   ├── indicators.py               # pure-pandas technical indicator functions
│   │   ├── heatmap.py                  # Heatmap tab renderer + CSV export
│   │   ├── sector_synopsis.py          # Sector Synopsis tab renderer
│   │   ├── stock_detail.py             # Stock Detail tab renderer + CSV export + badges
│   │   └── index_overlap.py            # Index Overlap tab renderer
│   ├── load_sectors.py                 # CLI: apply/check/export gics_sectors.json
│   ├── sync_constituents.py            # CLI: standalone constituent sync
│   └── main.py                         # CLI: daily orchestrator (sync + prices)
├── data/
│   └── gics_sectors.json               # static {symbol: gics_sector} map (532 symbols)
├── docs/
│   ├── user-guide.md                   # end-user guide for the dashboard
│   └── timescaledb-cheatsheet.md       # TimescaleDB SQL reference
├── AGENTS.md                           # this file
└── readme.md                           # human-facing setup guide
# scripts/ is gitignored — operator-only utilities (pg_dump backups, DB
# migration SQL with credentials) live there but are not part of the repo.
```

---

## Configuration

File: `src/config/configuration.json`

```json
{
  "db_url": "postgresql://user:pass@host:port/dbname",
  "marketdata_token": "your_marketstack_key",
  "days": 1000,
  "api_sleep_seconds": 0.5,
  "anthropic_api_key": "optional — only used for AI sector classification"
}
```

Loaded by `src/config/settings.py` → `load_settings()` returns a `Settings` object.  
Field names matter: use `db_url` and `marketdata_token` — not `database_url` or `marketstack_access_key`.

The dashboard searches four candidate paths for config (in order):
1. `src/config/config.json`
2. `src/config/configuration.json`
3. `config.json`
4. `configuration.json`

---

## Database Schema

All tables are in the `public` schema.

### `public.assets`
Primary metadata table. One row per symbol.

| Column          | Type        | Notes                              |
|-----------------|-------------|------------------------------------|
| symbol          | TEXT PK     |                                    |
| name            | TEXT        | Company display name               |
| exchange_code   | TEXT        |                                    |
| exchange        | TEXT        |                                    |
| asset_type      | TEXT        |                                    |
| price_currency  | TEXT        |                                    |
| last_refreshed  | DATE        | Date of most recent bar fetch      |
| gics_sector     | TEXT        | GICS sector string or NULL         |
| updated_at      | TIMESTAMPTZ |                                    |

### `public.daily_bars`
TimescaleDB hypertable. One row per symbol per trading day.

| Column      | Type        | Notes                   |
|-------------|-------------|-------------------------|
| symbol      | TEXT        | FK → assets             |
| ts          | TIMESTAMPTZ | PK with symbol          |
| open        | NUMERIC     |                         |
| high        | NUMERIC     |                         |
| low         | NUMERIC     |                         |
| close       | NUMERIC     |                         |
| volume      | BIGINT      |                         |
| adj_open    | NUMERIC     |                         |
| adj_high    | NUMERIC     |                         |
| adj_low     | NUMERIC     |                         |
| adj_close   | NUMERIC     |                         |
| adj_volume  | BIGINT      |                         |
| split_factor| NUMERIC     |                         |
| dividend    | NUMERIC     |                         |

All timestamp queries cast via `(ts AT TIME ZONE 'UTC')::date` to normalize to date.

### `public.sp500_constituents` / `nasdaq100_constituents` / `dow30_constituents`
Index membership tables. Schema differs slightly per table (column name for company differs),
but all share:

| Column       | Type        | Notes                                        |
|--------------|-------------|----------------------------------------------|
| symbol       | TEXT PK     |                                              |
| is_active    | BOOLEAN     | DEFAULT TRUE; FALSE = soft-deleted           |
| removed_date | DATE        | Set when soft-deleted                        |
| updated_at   | TIMESTAMPTZ |                                              |

The company name column is `security` in sp500_constituents and `company` in the other two.

**Active constituent query pattern:**
```sql
WHERE is_active IS NOT FALSE
```
This pattern (not `= TRUE`) handles NULL values in legacy rows that predate the column.

---

## Data Flow

### Daily Update (`src/main.py`)
1. Calls `ConstituentSyncService.sync_all()` (unless `--skip-sync`)
2. For newly added symbols, calls `ensure_sectors()` for classification
3. Fetches active symbols from all three index tables
4. Deduplicates across indices
5. For each symbol, calls `DailyBarImporter.import_symbol()`
6. Prints per-symbol counts and a summary of data freshness

### Constituent Sync (`src/services/constituent_sync.py`)
1. `fetch_remote(index_name)` → HTTP GET to yfiua GitHub Pages JSON
2. `fetch_current_active(index_name)` → DB query for active symbols
3. Diff: `additions = remote - active`, `removals = active - remote`
4. `upsert_additions()` → INSERT ON CONFLICT for constituent table + minimal assets row
5. `soft_delete_removals()` → UPDATE SET is_active=FALSE, removed_date=CURRENT_DATE
6. Returns `SyncResult(index_name, added, removed, unchanged)`

Dry run skips steps 4–5 but still runs `ensure_schema()`.

### Incremental Bar Import (`src/services/daily_bar_importer.py`)
1. `_latest_stored_bar_date(symbol)` → `SELECT max(ts)::date FROM daily_bars WHERE symbol = %s`
2. Computes days to fetch: `(today - last_date).days + 5` (5-day overlap buffer)
3. If 0 days needed, returns immediately
4. `client.fetch_daily(symbol, days)` → Marketstack EOD API
5. `repositories.upsert_asset()` → updates metadata
6. `repositories.upsert_daily_bars()` → bulk upsert via `executemany`
7. `conn.commit()`

Max days per request: 1000.

### Dashboard Data Query (`src/dashboard/data.py → fetch_treemap_data`)
SQL pattern:
1. CTE `universe` — joins assets to the relevant constituent table(s), filters `is_active IS NOT FALSE`
2. CTE `start_px` — `DISTINCT ON (symbol) ORDER BY ts ASC` within date range
3. CTE `end_px` — `DISTINCT ON (symbol) ORDER BY ts DESC` within date range
4. Final SELECT computes `return_pct` and `dollar_volume`
5. Rows with NULL `return_pct` or `dollar_volume` are dropped

---

## Dashboard Architecture

The dashboard is split into one entry-point module and six supporting modules:

| Module | Responsibility |
|--------|---------------|
| `app.py` | Page config, global CSS, sidebar controls, top-level data fetch, session-state tab navigation (4 tabs) |
| `data.py` | `connect`, `build_universe_sql`, `fetch_treemap_data`, `fetch_available_date_bounds`, `fetch_index_overlap`, `get_treemap_data_cached`, `get_ohlcv_cached`, LRU cache helpers |
| `charts.py` | `build_fig` (treemap), `build_detail_fig` (multi-panel candlestick) |
| `indicators.py` | Pure-pandas indicator computations — no Streamlit or DB dependencies |
| `heatmap.py` | `render_heatmap_tab` — KPI row, treemap/table toggle, CSV export button |
| `sector_synopsis.py` | `render_sector_synopsis_tab` (entry point) + `render_sector_synopsis` (`@st.fragment`) |
| `stock_detail.py` | `render_stock_detail` (`@st.fragment`) — symbol picker, index membership badges, candlestick chart, OHLCV CSV export |
| `index_overlap.py` | `render_index_overlap_tab` — headline counts, cross-membership bar chart, per-bucket symbol tables |

### Tab Navigation

Tabs are implemented as three `st.button` widgets backed by `session_state["active_tab"]`.
This replaces `st.tabs`, which resets to tab 0 on every full page rerun in Streamlit 1.53.

```python
_TABS = ["Heatmap", "Sector Synopsis", "Stock Detail"]
# Buttons use type="primary"/"secondary" based on active_tab.
# _on_tab_click callback sets session_state["active_tab"] before the rerun.
```

---

## Key Functions Reference

### `src/dashboard/app.py`

| Function | Signature | Purpose |
|----------|-----------|---------|
| `load_config` | `() -> dict` | Searches 4 candidate paths for config JSON |
| `_preset_dates` | `(key: str) -> (date, date)` | Compute (date_from, date_to) for a preset key |
| `_on_preset_click` | `(label, min_day, end_max_day) -> None` | `on_click` callback for preset buttons; updates session_state before rerun |
| `_on_tab_click` | `(tab_name: str) -> None` | `on_click` callback for tab nav buttons |
| `main` | `() -> None` | Streamlit entry point |

### `src/dashboard/data.py`

| Function | Signature | Purpose |
|----------|-----------|---------|
| `connect` | `(db_url: str)` | Returns a psycopg connection |
| `build_universe_sql` | `(index_key: str) -> str` | Returns CTE SQL for the selected universe |
| `fetch_treemap_data` | `(conn, index_key, date_from, date_to) -> DataFrame` | Core heatmap query |
| `fetch_available_date_bounds` | `(db_url, index_key) -> (date, date)` | Min/max available bar dates; `@st.cache_data` TTL=60s |
| `fetch_index_overlap` | `(db_url) -> DataFrame` | Per-symbol boolean index-membership flags; `@st.cache_data` TTL=3600s. Columns: `symbol`, `name`, `sector`, `in_sp500`, `in_nasdaq100`, `in_dow30` |
| `_get_session_cache` | `() -> OrderedDict` | Returns (or creates) the treemap LRU cache from session_state |
| `get_treemap_data_cached` | `(db_url, index_key, date_from, date_to, cache_size=24) -> DataFrame` | Session-level LRU wrapper for treemap data |
| `_get_ohlcv_cache` | `() -> OrderedDict` | Returns (or creates) the OHLCV LRU cache from session_state |
| `get_ohlcv_cached` | `(db_url, symbol, date_from, date_to, cache_size=20) -> DataFrame` | Session-level LRU wrapper for OHLCV data |

### `src/dashboard/charts.py`

| Function | Signature | Purpose |
|----------|-----------|---------|
| `build_fig` | `(df, color_range) -> Figure` | Builds Plotly treemap (RdYlGn, sized by dollar volume) |
| `build_detail_fig` | `(df, symbol, active: list[str]) -> Figure` | Builds dynamic multi-panel candlestick figure |

`build_detail_fig` layout rules:
- **Row 1**: Candlestick + price overlays (SMA 20/50, EMA 20, Bollinger Bands)
- **Row 2**: Volume bars (green/red)
- **Rows 3+**: Optional sub-panels in fixed order — RSI → MACD → ATR → OBV
- Row heights and figure height computed dynamically from the number of active sub-panels.

### `src/dashboard/indicators.py`

All functions are pure pandas — no Streamlit or DB imports.

| Function | Signature | Returns | Min bars |
|----------|-----------|---------|----------|
| `compute_sma` | `(close: Series, window: int)` | `Series` | `window` |
| `compute_ema` | `(close: Series, window: int)` | `Series` | `window` |
| `compute_bollinger_bands` | `(close: Series, window=20, num_std=2.0)` | `DataFrame`: `bb_upper`, `bb_mid`, `bb_lower` | 21 |
| `compute_rsi` | `(close: Series, window=14)` | `Series` named `"RSI"` | 15 |
| `compute_macd` | `(close: Series, fast=12, slow=26, signal=9)` | `DataFrame`: `macd`, `signal`, `histogram` | 35 |
| `compute_atr` | `(high, low, close: Series, window=14)` | `Series` named `"ATR"` | 15 |
| `compute_obv` | `(close: Series, volume: Series)` | `Series` named `"OBV"` | 2 |

### `src/dashboard/heatmap.py`

| Function | Signature | Purpose |
|----------|-----------|---------|
| `render_ranked_table` | `(df, color_range) -> None` | Renders sortable table of all stocks by return % with RdYlGn coloring |
| `_build_export_csv` | `(df) -> bytes` | Builds in-memory CSV: symbol, name, sector, return_pct, dollar_volume, percentile_rank |
| `_exceeds_three_months` | `(d_from, d_to) -> bool` | Calendar-month guard — True if span is strictly > 3 months |
| `render_heatmap_tab` | `(df, color_range, range_label, index_key, date_from, date_to) -> None` | Full Heatmap tab: KPI row, treemap/table toggle, CSV export button |

CSV export is disabled (`disabled=True`, empty data) when the date range exceeds 3 calendar months.

### `src/dashboard/sector_synopsis.py`

| Function | Signature | Purpose |
|----------|-----------|---------|
| `_fmt_dollar` | `(v: float) -> str` | Compact dollar formatter: `$1.2B` / `$450.3M` / `$12.5K` / `$999` |
| `render_sector_synopsis` | `(df, sector, range_label, db_url, date_from, date_to) -> None` | `@st.fragment` — KPI row + ranked bar chart + inline stock detail on click |
| `render_sector_synopsis_tab` | `(df, range_label, db_url, date_from, date_to) -> None` | Tab entry point — sector selectbox + calls fragment |

KPI row uses `st.caption` for the period label (above the metrics) and custom column widths `[0.7, 1.6, 1.6, 1.4, 1.7]` so "Stocks" doesn't over-claim space. The selectbox is **outside** the fragment so changing the sector triggers a full rerun.

### `src/dashboard/stock_detail.py`

| Function | Signature | Purpose |
|----------|-----------|---------|
| `_exceeds_three_months` | `(d_from, d_to) -> bool` | Calendar-month guard (same logic as heatmap.py) |
| `render_stock_detail` | `(df, db_url, date_from, date_to) -> None` | `@st.fragment` — symbol picker, index membership badges, indicator multiselect, candlestick chart, OHLCV CSV export |

Symbol selection design:
- Stores only the raw ticker in `session_state["detail_selected_ticker"]` (not the full display label).
- Uses `index=` on `st.selectbox` — no `key=` — to bypass Streamlit's options-list validation at widget init time.
- Full display labels include return % which changes when the date range changes; storing only the ticker prevents stale-label exceptions.

Index membership badges: HTML `<span>` pills rendered via `st.markdown(unsafe_allow_html=True)`. Data comes from `fetch_index_overlap(db_url)` — already cached, zero extra DB calls.

### `src/dashboard/index_overlap.py`

| Function | Signature | Purpose |
|----------|-----------|---------|
| `render_index_overlap_tab` | `(db_url: str) -> None` | Full Index Overlap tab: headline counts, cross-membership bar chart, per-bucket symbol tables |

Seven membership buckets (all mutual-exclusion combinations of S&P 500 / NASDAQ-100 / Dow 30). Each bucket has a label, colour, count, and a filtered symbol DataFrame. Displayed as a horizontal Plotly bar chart sorted by the fixed bucket order, followed by expandable `st.dataframe` per bucket. Uses `fetch_index_overlap` (1-hour cache).

### `src/services/constituent_sync.py`

| Method | Notes |
|--------|-------|
| `ensure_schema()` | Idempotent `ALTER TABLE ADD COLUMN IF NOT EXISTS`; always call before sync or dry-run |
| `fetch_remote(index_name)` | Returns `list[tuple[str, str]]` (symbol, name) |
| `fetch_current_active(index_name)` | Returns `set[str]` |
| `sync_index(index_name)` | Full sync for one index; returns `SyncResult` |
| `sync_all()` | Syncs sp500, nasdaq100, dow30; returns `list[SyncResult]` |
| `dry_run_index(index_name)` | Read-only diff; returns `SyncResult` |
| `dry_run_all()` | Read-only diff for all three; returns `list[SyncResult]` |

yfiua URLs:
- sp500: `https://yfiua.github.io/index-constituents/constituents-sp500.json`
- nasdaq100: `https://yfiua.github.io/index-constituents/constituents-nasdaq100.json`
- dow30: `https://yfiua.github.io/index-constituents/constituents-dowjones.json`

### `src/services/sector_classifier.py`

| Symbol | Notes |
|--------|-------|
| `GICS_SECTORS` | `list[str]` of 11 valid GICS sector strings |
| `ensure_sectors(conn, new_symbols, api_key)` | Tier 1: static file; Tier 2: Claude API. Returns count classified |
| `classify_via_api(symbols, api_key)` | Direct Claude API call; only used if static file misses |

Static file path: `data/gics_sectors.json` — `{symbol: gics_sector}` flat mapping.

### `src/db/repositories.py`

| Function | Notes |
|----------|-------|
| `upsert_asset(conn, asset_row: dict)` | ON CONFLICT preserves existing `gics_sector` if new value is NULL |
| `upsert_daily_bars(conn, bars: list[dict])` | `executemany`; only updates rows where any OHLCV field is `DISTINCT FROM` existing |
| `fetch_sp500_symbols(conn)` | `WHERE is_active IS NOT FALSE` |
| `fetch_nasdaq100_symbols(conn)` | `WHERE is_active IS NOT FALSE` |
| `fetch_dow30_symbols(conn)` | `WHERE is_active IS NOT FALSE` |
| `fetch_ohlcv(conn, symbol, date_from, date_to)` | Returns DataFrame: date, open, high, low, close, volume |

### `src/marketdata/client.py`

| Method | Notes |
|--------|-------|
| `fetch_daily(symbol, days=1000)` | Fetches most recent N trading days; paginates |
| `fetch_daily_range(symbol, date_from, date_to)` | Explicit date range; paginates; returns sorted oldest→newest |
| `load_payload_from_file(path)` | Load a saved Marketstack JSON response from disk |

Exchange hardcoded to `NASDAQ` in `fetch_daily`. `fetch_daily_range` accepts `exchange` param (default `NASDAQ`).

---

## CLI Commands

```bash
# Daily update (constituent sync + price fetch for all active symbols)
python -m src.main
python -m src.main --skip-sync          # skip constituent sync step

# Standalone constituent sync
python -m src.sync_constituents
python -m src.sync_constituents --dry-run
python -m src.sync_constituents --skip-sector

# Historical backfill (one-time)
python -m src.backfill.backfill_10y
python -m src.backfill.backfill_10y --years 5

# Sector management
python -m src.load_sectors              # apply gics_sectors.json → DB
python -m src.load_sectors --check      # report symbols missing sectors
python -m src.load_sectors --export     # dump DB sectors → gics_sectors.json

# Dashboard
streamlit run src/dashboard/app.py
```

---

## Caching Architecture

### `@st.cache_data` (cross-session, Streamlit-managed)
- `fetch_available_date_bounds`: TTL = 60 seconds
- `fetch_index_overlap`: TTL = 3600 seconds — constituent membership is stable between daily syncs; shared across all browser sessions

### Session-level LRU (per browser tab, stored in `st.session_state`)
Two separate `OrderedDict` caches in `src/dashboard/data.py`, evicted by LRU:

| Cache | Key | Max entries |
|-------|-----|-------------|
| `treemap_cache` | `(db_url, index_key, date_from_iso, date_to_iso)` | 24 |
| `ohlcv_cache` | `(symbol, date_from_iso, date_to_iso)` | 20 |

Stats (`treemap_cache_hits`, `treemap_cache_misses`) are tracked in session_state.  
Both caches are cleared together by the "Clear cached results" sidebar button.

---

## Streamlit Fragment Boundaries

Two functions are decorated with `@st.fragment`:

### `render_sector_synopsis` (`sector_synopsis.py`)
- Bar clicks (`on_select="rerun"`) trigger a **fragment rerun only**.
- The active tab and sector selectbox (outside the fragment) are unaffected.
- The sector selectbox is intentionally **outside** the fragment so changing sector triggers a full rerun to re-slice `df`.

### `render_stock_detail` (`stock_detail.py`)
- Symbol and indicator changes trigger a **fragment rerun only**.
- The active tab is never reset by symbol changes or chart interactions.
- Uses `index=` (no `key=`) on the symbol selectbox to avoid Streamlit's options-list validation firing before the stale label can be corrected.

Fragment parameters are passed by the full-page render and are frozen for the lifetime of each fragment rerun (they come from the last full render's arguments).

---

## Known Behavioral Constraints

1. **Treemap drill-down state is ephemeral.** Plotly stores drill-down navigation client-side only. Any Streamlit rerun (e.g., changing sidebar filters, clicking the raw data expander) resets the treemap to its root view.

2. **`on_select` does not fire on treemap clicks.** Treemap clicks are consumed as Plotly drill-down navigation events, not selection events. `on_select="rerun"` is only effective on non-treemap chart types (e.g., bar, scatter).

3. **`is_active IS NOT FALSE` vs `= TRUE`.** All active-constituent filters use `IS NOT FALSE` to handle legacy NULL rows predating the `is_active` column. Do not change to `= TRUE`.

4. **Bar upsert is conditional.** `upsert_daily_bars` only writes when at least one OHLCV field has changed (`IS DISTINCT FROM`). This avoids unnecessary write amplification on re-runs.

5. **`gics_sector` is preserved on asset upsert.** `COALESCE(EXCLUDED.gics_sector, public.assets.gics_sector)` ensures that fetching new price data never clears a previously classified sector.

6. **Max 1000 bars per Marketstack request.** `DailyBarImporter._MAX_DAYS_PER_REQUEST = 1000`. Backfill uses chunked date-range requests (900 days per chunk) to stay within this limit.

7. **Backfill skips symbols already covered.** If `earliest_day(symbol) <= target_start`, the symbol is skipped entirely.

8. **`ensure_schema()` must be called before any sync or dry-run.** Constituent queries use `is_active IS NOT FALSE`, which fails if the column doesn't exist yet.

9. **`st.tabs` is not used.** Streamlit 1.53 resets `st.tabs` to tab 0 on every full page rerun and does not support a `key=` parameter. The dashboard uses session-state button navigation instead — four `st.button` widgets with `type="primary"/"secondary"` controlled by `session_state["active_tab"]`.

10. **Date inputs use `key=` with pre-widget clamping.** `key="date_from"` / `key="date_to"` make widget state and session_state the same object — manual changes apply immediately (no "two-click" lag). Session_state values are clamped to the available data range *before* the widgets render; writing to `session_state[key]` after the widget is rendered raises `StreamlitAPIException`. Preset-button `on_click` callbacks run before the rerun so the widget picks up the preset dates correctly.

11. **CSV export is rate-limited to 3 calendar months.** Both the Heatmap and Stock Detail download buttons are disabled when `date_to > date_from + 3 calendar months`. The check is calendar-month-aware (not a flat 90-day count): `_exceeds_three_months(d_from, d_to)` in `heatmap.py` and `stock_detail.py`.

---

## GICS Sectors (valid values for `assets.gics_sector`)

```
Communication Services
Consumer Discretionary
Consumer Staples
Energy
Financials
Health Care
Industrials
Information Technology
Materials
Real Estate
Utilities
```

Any value not in this list is rejected by `apply_sectors()` in `src/load_sectors.py`.

---

## Index Universe Keys

| UI Label  | `index_key` | Constituent table             |
|-----------|-------------|-------------------------------|
| S&P 500   | `sp500`     | `public.sp500_constituents`   |
| NASDAQ-100| `nasdaq100` | `public.nasdaq100_constituents`|
| Dow 30    | `dow30`     | `public.dow30_constituents`   |
| All       | `all`       | union of all three (deduped)  |

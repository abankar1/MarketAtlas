# MarketAtlas — Agent Context

This file describes the MarketAtlas codebase for AI agents. It covers architecture,
data flow, every significant file and function, database schema, CLI contracts,
caching, and known behavioral constraints.

---

## Project Identity

**Type:** Streamlit dashboard + Python data pipeline + Anthropic LLM-backed Ask AI tab  
**Purpose:** Track and visualize price performance of S&P 500, NASDAQ-100, and Dow 30 constituents; answer natural-language questions about historical price/volume.  
**Stack:** Python 3.12 (cloud) / 3.14 (local), Streamlit, TimescaleDB (PostgreSQL on Tigerdata Cloud), psycopg3, Plotly, Pandas, Marketstack API, Anthropic API.  
**Hosting:** Streamlit Community Cloud serves the dashboard from the `release` branch; GitHub Actions runs the daily-update job (cron in `.github/workflows/daily-update.yml`) from `main`.  
**Working directory:** repo root (all `python -m` commands run from here)

---

## Repository Layout

```
repo root/
├── src/
│   ├── config/
│   │   ├── configuration.json          # real credentials — git-ignored (local-dev fallback)
│   │   ├── configuration.json.example  # committed placeholder
│   │   └── settings.py                 # 3-source loader: st.secrets → env vars → JSON file
│   ├── db/
│   │   ├── connection.py               # psycopg.connect() wrapper
│   │   ├── readonly.py                 # sqlglot validator + readonly executor (Ask AI tab)
│   │   └── repositories.py             # all SQL — upserts, fetches, OHLCV, nl_queries log, usage_events log
│   ├── ai/
│   │   ├── __init__.py
│   │   ├── client.py                   # Anthropic Messages API wrapper (httpx)
│   │   ├── query_templates.py          # pre-approved SQL templates + render() (Ask AI primary path)
│   │   ├── intent_router.py            # Claude call: question → template name + params
│   │   ├── nl_to_sql.py                # free-form NL → SQL fallback
│   │   ├── narrate.py                  # 1-line conversational summary of a SQL result
│   │   ├── memory.py                   # ConversationTurn + transcript helpers (multi-turn Ask AI memory)
│   │   └── cache.py                    # process-wide LRU+TTL caches; PROMPT_VERSION invalidation
│   ├── marketdata/
│   │   └── client.py                   # Marketstack REST client
│   ├── services/
│   │   ├── constituent_sync.py         # live sync against yfiua GitHub Pages
│   │   ├── daily_bar_importer.py       # incremental OHLCV importer
│   │   └── sector_classifier.py        # GICS sector logic + Claude API fallback
│   ├── backfill/
│   │   └── backfill_10y.py             # one-time historical backfill
│   ├── dashboard/
│   │   ├── app.py                      # Streamlit entry point — config, sidebar, nav, usage_events logging
│   │   ├── data.py                     # DB queries + session-level LRU caches
│   │   ├── charts.py                   # Plotly figure builders (treemap + candlestick)
│   │   ├── indicators.py               # pure-pandas technical indicator functions
│   │   ├── prefs.py                    # load/save ~/.marketatlas/prefs.json (sidebar defaults)
│   │   ├── heatmap.py                  # Heatmap tab renderer + top-5 movers strip + CSV export
│   │   ├── sector_synopsis.py          # Sector Synopsis tab renderer
│   │   ├── stock_detail.py             # Stock Detail tab renderer + CSV export + badges
│   │   ├── index_overlap.py            # Index Overlap tab renderer (HIDDEN — see _TABS)
│   │   ├── news.py                     # News tab — Marketaux per-symbol headlines + sentiment
│   │   └── ask.py                      # Ask AI tab — multi-turn router → template OR AI-SQL fallback
│   ├── load_sectors.py                 # CLI: apply/check/export gics_sectors.json
│   ├── sync_constituents.py            # CLI: standalone constituent sync
│   └── main.py                         # CLI: daily orchestrator (sync + prices)
├── data/
│   └── gics_sectors.json               # static {symbol: gics_sector} map (532 symbols)
├── docs/
│   ├── user-guide.md                   # End-user dashboard guide
│   ├── deployment.md                   # Streamlit Cloud + Tigerdata Cloud + GHA runbook
│   └── timescaledb-cheatsheet.md       # TimescaleDB SQL reference
├── tests/
│   ├── test_ai_cache.py                # Cache key + TTL tests
│   ├── test_intent_router.py           # Routing + parsing tests
│   ├── test_nl_to_sql_parsing.py       # response-parsing tests
│   ├── test_query_templates.py         # template render + ParamSpec tests
│   └── test_validate_sql.py            # SQL validator safety tests
├── .github/workflows/
│   └── daily-update.yml                # GHA cron: market data refresh M/W/F at 09:00 UTC
├── .streamlit/
│   ├── config.toml                     # toolbarMode = "minimal"
│   ├── secrets.toml.example            # committed template for Streamlit Cloud secrets UI
│   └── secrets.toml                    # git-ignored — real secrets if testing locally
├── AGENTS.md                           # this file
└── readme.md                           # human-facing setup guide
# scripts/ is gitignored — operator-only files (DB setup SQL with passwords,
# migrations, backup helpers) live there but are not part of the published repo.
```

---

## Configuration

`src/config/settings.py::load_settings()` reads from three sources, **in priority
order** — the first source with a non-empty `db_url` wins:

1. **`st.secrets`** — Streamlit Community Cloud (entered via the app's
   *Settings → Secrets* UI; or `.streamlit/secrets.toml` for local Streamlit
   testing). Keys may be flat or nested under `[market_atlas]`.
2. **Environment variables** (uppercase form: `DB_URL`, `MARKETDATA_TOKEN`,
   `ANTHROPIC_API_KEY`, …) — used by the GitHub Actions daily-update job.
3. **`src/config/configuration.json`** — local-dev fallback (git-ignored).

The same code therefore runs unchanged on Streamlit Cloud, in GHA, and locally.

### Required + optional fields

```jsonc
{
  "db_url": "postgresql://user:pass@host:port/dbname",                  // required
  "marketdata_token": "your_marketstack_key",                            // required
  "db_url_readonly": "postgresql://atlas_reader:...@host:port/dbname",  // optional — Ask AI safety
  "days": 1000,
  "api_sleep_seconds": 0.2,
  "anthropic_api_key": "sk-ant-...",            // required for Ask AI + sector classifier
  "anthropic_model": "claude-haiku-4-5",
  "marketaux_token": "..."                      // optional — News tab headlines
}
```

Field names matter: use `db_url` and `marketdata_token` — not `database_url` or
`marketstack_access_key`.

`db_url_readonly` is the connection string for the `atlas_reader` Postgres role
used by the Ask AI tab to execute LLM-generated SQL. The role has SELECT-only
privileges and a per-connection `statement_timeout=15s` set by `execute_safe()`.
A template for creating this role lives in `docs/deployment.md` Phase 1.

The example template `.streamlit/secrets.toml.example` is committed; the real
`secrets.toml` is git-ignored.

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

### `public.nl_queries`
Audit log for the Ask AI tab. One row per natural-language question, success or failure.

| Column                  | Type        | Notes                                       |
|-------------------------|-------------|---------------------------------------------|
| id                      | BIGSERIAL PK |                                            |
| ts                      | TIMESTAMPTZ | DEFAULT NOW()                               |
| question                | TEXT        | Raw user input                              |
| generated_sql           | TEXT        | Rendered template SQL or AI-generated SQL; NULL if generation failed |
| status                  | TEXT        | success / unsafe_sql / timeout / cannot_answer / generation_error / execution_error / ai_error |
| error_message           | TEXT        |                                             |
| row_count               | INTEGER     |                                             |
| duration_ms             | INTEGER     |                                             |
| input_tokens            | INTEGER     | Total across router + (if invoked) AI-SQL generator |
| output_tokens           | INTEGER     |                                             |
| cache_read_tokens       | INTEGER     | Prompt cache hits                           |
| cache_creation_tokens   | INTEGER     | Prompt cache writes                         |
| path                    | TEXT        | `'template'` or `'ai_sql'` — which path served the question |
| template_name           | TEXT        | Matched template (e.g. `'sector_count_by_index'`); NULL on AI-SQL path |
| template_params         | JSONB       | Bound parameter dict for replay; NULL on AI-SQL path |
| from_cache              | BOOLEAN     | TRUE if served from the in-process LLM cache (no API call) |
| raw_response            | TEXT        | Raw model output (router JSON or `<sql>` block) for replay/debugging |

Created by `scripts/create_nl_queries_table.sql` then extended by follow-up
migration scripts. Indexed on `ts DESC`, `status`, `path`, `template_name`, and
`from_cache`.

### `public.usage_events`
Lightweight lifecycle log: one row per session load, one row per tab click.
Auto-created on first insert by `_ensure_usage_events_table()` in `repositories.py`.

| Column     | Type        | Notes                                        |
|------------|-------------|----------------------------------------------|
| id         | BIGSERIAL PK |                                             |
| ts         | TIMESTAMPTZ | DEFAULT NOW()                                |
| session_id | TEXT        | UUID generated once per browser session in `app.py` |
| event_type | TEXT        | `'session_load'` or `'tab_change'`           |
| from_tab   | TEXT        | `NULL` for `session_load`; previous tab for `tab_change` |
| to_tab     | TEXT        | Initial tab (`session_load`) or destination (`tab_change`) |

Indexed on `ts DESC` and `session_id`. Writes are fire-and-forget via daemon
threads so a tab click is never blocked on cloud-DB latency. See `log_usage_event`
in `repositories.py`.

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

The dashboard is split into one entry-point module and several supporting modules:

| Module | Responsibility |
|--------|---------------|
| `app.py` | Page config, global CSS, sidebar controls, top-level data fetch, session-state tab navigation, lifecycle logging (`session_load` / `tab_change` → `usage_events`) |
| `data.py` | `connect`, `build_universe_sql`, `fetch_treemap_data`, `fetch_available_date_bounds`, `fetch_index_overlap`, `get_treemap_data_cached`, `get_ohlcv_cached`, LRU cache helpers |
| `charts.py` | `build_fig` (treemap), `build_detail_fig` (multi-panel candlestick), `build_compare_fig` (multi-symbol rebased) |
| `indicators.py` | Pure-pandas indicator computations — no Streamlit or DB dependencies |
| `prefs.py` | `load_prefs` / `save_prefs` for `~/.marketatlas/prefs.json` (sidebar defaults survive browser restarts) |
| `heatmap.py` | `render_heatmap_tab` — KPI row, top-5 movers strip (▲/▼), treemap/table toggle, CSV export |
| `sector_synopsis.py` | `render_sector_synopsis_tab` (entry point) + `render_sector_synopsis` (`@st.fragment`) |
| `stock_detail.py` | `render_stock_detail` (`@st.fragment`) — symbol picker, index membership badges, candlestick chart, OHLCV CSV export, Compare-mode multi-symbol chart |
| `index_overlap.py` | `render_index_overlap_tab` — present in code but **hidden** from the tab strip (still renderable if the tab is re-added to `_TABS`) |
| `news.py` | `render_news_tab` — Marketaux per-symbol headlines + sentiment chips |
| `ask.py` | `render_ask_tab` — multi-turn router → template OR free-form NL→SQL fallback → `validate_sql` → `execute_safe` on the readonly role → dataframe + 1-line narrative summary |

### Tab Navigation

Tabs are implemented as `st.button` widgets backed by `session_state["active_tab"]`.
This replaces `st.tabs`, which resets to tab 0 on every full page rerun in Streamlit 1.53.

```python
# src/dashboard/app.py
# Index Overlap is intentionally omitted from the visible strip; its render
# branch and import remain in place so re-enabling is a one-line change.
_TABS = ["Heatmap", "Ask AI", "Sector Synopsis", "Stock Detail", "News"]
# Buttons use type="primary"/"secondary" based on active_tab.
# _on_tab_click callback sets session_state["active_tab"] AND fires a
# fire-and-forget log_usage_event(..., 'tab_change', ...) on a daemon thread.
```

---

## Key Functions Reference

### `src/dashboard/app.py`

| Function | Signature | Purpose |
|----------|-----------|---------|
| `load_config` | `() -> dict` | Delegates to `src.config.settings.load_settings()` (st.secrets / env vars / JSON file). Returns `{}` on missing config so caller can show a friendly error. |
| `_preset_dates` | `(key: str) -> (date, date)` | Compute (date_from, date_to) for a preset key |
| `_on_preset_click` | `(label, min_day, end_max_day) -> None` | `on_click` callback for preset buttons; updates session_state before rerun |
| `_on_tab_click` | `(tab_name: str, db_url: str = "") -> None` | `on_click` callback for tab nav buttons; sets active_tab AND fires `log_usage_event(..., 'tab_change', ...)` (fire-and-forget on a daemon thread) |
| `main` | `() -> None` | Streamlit entry point. Generates `_session_id` (UUID) once per session. Logs `session_load` (with `to_tab='Heatmap'`) once per session, gated by `_app_load_persisted`. |

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
| `log_nl_query(db_url, *, question, generated_sql=None, status, ...)` | Synchronous best-effort INSERT into `nl_queries`. Uses primary `db_url` (the readonly role has no INSERT). Connect timeout 3s. Swallows all exceptions (audit logging must never break the UI). |
| `log_usage_event(db_url, *, session_id, event_type, from_tab=None, to_tab=None)` | **Fire-and-forget** — spawns a daemon thread to do the DB write so the UI returns in microseconds. The actual blocking write lives in `_log_usage_event_sync`. |
| `_ensure_usage_events_table(conn)` | Idempotent `CREATE TABLE IF NOT EXISTS usage_events …` + indices. Module-level flag `_USAGE_EVENTS_ENSURED` skips the no-op DDL after first success per Python process. |

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
Both caches are cleared together by the "Clear cached results" sidebar button (developer-only).

### LLM decision caches (process-wide, `src/ai/cache.py`)
Three module-level `TTLCache` instances (12h TTL, 200 entries each) — survive Streamlit reruns within the same worker process; lost on process restart:

| Cache | Stores | Purpose |
|-------|--------|---------|
| `_ROUTE_CACHE` | `RouteCacheEntry(kind, name, params)` | Skip a router-Claude call on repeat questions |
| `_AI_SQL_CACHE` | Generated SQL string | Skip an AI-SQL-generator call on repeat questions |
| `_NARRATE_CACHE` (in `narrate.py`) | One-line narrative string | Skip a narrate-Claude call when the same question has the same result rows |

All keys fold `PROMPT_VERSION` + `model_id` + `last_ticker` + `transcript_hash` + `normalised_question` so:
- Different conversations don't collide on identical follow-ups (e.g. "their names" after different sets of symbols).
- Bumping `PROMPT_VERSION` invalidates every stale entry on the next deploy. The history is documented inline in `cache.py`. Bumping is essentially the only way to flush after a prompt or template edit, and it costs one character.

The dashboard's developer-only "Clear AI cache" sidebar button calls `clear_all()` to wipe all three.

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

12. **AI-generated SQL runs ONLY on the readonly role.** `db_url_readonly` is a separate Postgres role (`atlas_reader`) with `SELECT`-only privileges. `execute_safe()` sets `statement_timeout=15000ms` per-connection and `SET TRANSACTION READ ONLY` per query. Audit log writes use `db_url` (the primary role) because the readonly role has no INSERT privilege.

13. **SQL validation uses sqlglot, not regex.** `validate_sql` parses the AST and walks every node, rejecting any non-SELECT/WITH/UNION/INTERSECT/EXCEPT statement type *anywhere in the tree* (catches DML embedded in CTEs, e.g., `WITH d AS (DELETE FROM x RETURNING *) SELECT ...`), plus a deny-list of forbidden function names (`pg_read_file`, `pg_sleep`, `dblink`, `pg_terminate_backend`, …). Multi-statement input (semicolon-separated) is rejected before parsing.

14. **System prompts are cached via `cache_control: ephemeral`.** The NL-to-SQL system prompt is ~7.5k chars (≈ 2-3k tokens); caching drops repeat-call cost to ~10% of cold cost within a 5-minute window. The user question is passed in the `messages` array — never interpolated into the system prompt — so the cache prefix stays byte-identical across calls.

15. **Templates are the primary path; AI-SQL is the fallback.** Operators should grow the registry in `src/ai/query_templates.py` rather than relying on the model writing SQL. The model never sees template SQL bodies — its job in the template path is intent classification + parameter extraction only. Numeric params are inlined into SQL only after a strict type+range check; string/date params are bound by psycopg.

16. **A template's parameter surface is its safety contract.** Adding a new param: declare a `ParamSpec` with `choices` (allowlist) for enums, `min`/`max` for numerics, or `pattern` for strings. The `render()` helper rejects anything outside the spec before any SQL is sent to the DB.

17. **The Ask AI tab caches LLM decisions, never DB results.** Three LRU+TTL caches in `src/ai/cache.py` (route, ai_sql, narrate). Keys fold `PROMPT_VERSION` so a one-character bump invalidates every stale entry on the next deploy — far more robust than manual cache clears. Templates re-execute against current DB state every time. Errors are not cached.

18. **Filter `daily_bars.ts` directly — never wrap it in a function inside WHERE.** TimescaleDB chunk pruning only kicks in when the partition column appears unwrapped in the WHERE clause. `WHERE (ts AT TIME ZONE 'UTC')::date >= …` disables pruning and turns a 90ms query into a 5+ second full-hypertable scan. Use `WHERE ts >= NOW() - INTERVAL '30 days'` instead. The schema docstring in `nl_to_sql.py` calls this out for the LLM as well.

19. **Hide Streamlit's "Ask Google" / "Ask ChatGPT" helper anchors.** Streamlit 1.31+ injects them next to `st.error` / `st.exception` widgets. They surface internal SQL/error text to third-party AI tools. Global CSS in `app.py` hides any anchor with `href*="google.com/search"`, `chatgpt.com/?q="`, or `chat.openai.com/?q="`. The native "Copy" button is preserved.

20. **`log_usage_event` is fire-and-forget.** The DB write happens on a daemon thread so the on_click handler returns in microseconds. Daemon threads die with the process, so an event in flight when the worker exits is lost — acceptable for low-volume lifecycle events. `log_nl_query` stays synchronous because the LLM call dominates that path's latency anyway.

21. **`PROMPT_VERSION` history is the changelog for prompt-affecting edits.** Every time prompts in `nl_to_sql.py`, `narrate.py`, or templates in `query_templates.py` change in a way that affects model output, bump `PROMPT_VERSION` in `cache.py` and add a one-line history entry. This invalidates stale cached entries deterministically — no operator action needed.

---

## AI Layer

Claude integration is organized under `src/ai/`:

- `client.py` — `AIClient` wrapping the Anthropic Messages API via `httpx`. Supports prompt caching via `cacheable_system=True` (marks the system block with `cache_control={"type": "ephemeral"}`). Returns `AIResponse` with input/output/cache token counts.
- `query_templates.py` — registry of **pre-approved parameterized SQL templates** (the Ask AI tab primary path). Each `QueryTemplate` declares a `name`, `description`, `sql` (with psycopg `%(name)s` placeholders for strings/dates and `{name}` placeholders for ints/floats), a typed `ParamSpec` per param (allowlist for sectors / indices, min/max for numerics, regex for symbols), and 1-3 NL examples. `render(template_name, params)` validates every param then returns `(sql, bound_params)`. Module import runs every template through sqlglot to catch authoring bugs at startup.
- `intent_router.py` — single Claude call that maps a question to either a template name + extracted params, or `null` (no match). The model **never sees the SQL bodies of templates** — only their names, descriptions, and NL examples. Output is JSON wrapped in `<json>...</json>` tags. Accepts `recent_turns` for multi-turn context.
- `nl_to_sql.py` — free-form NL→SQL **fallback** generator (used only when the router returns `null`). Holds the system prompt with schema DDL + 11-sector GICS enum + few-shot examples including a CANNOT_ANSWER refusal (sentence-cased, sentence-terminated).
- `narrate.py` — `summarize(client, question, columns, rows, last_ticker=None, recent_turns=None)`: a short Haiku call that turns SQL result rows into a 1-2 sentence answer in stock context. Cached on `(question, columns, rows-hash, last_ticker, transcript_hash)`. The system prompt also instructs the narrator to optionally append a "See the **Stock Detail** tab for the full chart." pointer when another tab would show meaningfully more.
- `memory.py` — `ConversationTurn` dataclass + `format_transcript()` and `transcript_hash()` helpers. The Ask AI tab keeps a sliding window of `MAX_TURNS=3` recent turns (each = `{question, top_symbols, summary}`) in `st.session_state.ask_recent_turns`; the transcript is injected into router / nl_to_sql / narrate prompts so referential follow-ups like "their names" resolve when `last_ticker` alone isn't enough.
- `cache.py` — process-wide LRU+TTL cache (12h TTL, 200 entries each) for two surfaces: (a) the router's decision (`{template, params}` or `miss`) and (b) the AI-SQL generator's output SQL. Keys fold `PROMPT_VERSION` + `model_id` + `last_ticker` + `transcript_hash` + `normalised_question`. Repeat questions skip the API call entirely; cached entries log `input_tokens=0` so audit reports show free queries clearly. The cache holds *only* the LLM's decision — actual SQL still runs against current DB state every time. **Bumping `PROMPT_VERSION` invalidates every stale entry on the next deploy without a manual cache clear** — a one-character change is enough.

The Ask AI tab orchestrator (`src/dashboard/ask.py::_run_query`):

1. **Route** — `intent_router.route(client, question, last_ticker=..., recent_turns=...)` → `RoutedTemplate` or `RoutingMiss`. Cache lookup happens first via `lookup_route`.
2. **Template path** (`RoutedTemplate`):
   a. `query_templates.render(name, params)` validates params, returns `(sql, bound_params)`.
   b. `execute_safe(db_url_readonly, sql, bound_params=...)` runs on the readonly role.
   c. `log_nl_query(..., path='template', template_name=..., template_params=...)`.
   d. `_attach_narrative(...)` calls `narrate.summarize(...)` for the 1-line summary.
3. **AI-SQL fallback** (`RoutingMiss`):
   a. `lookup_ai_sql` then `generate_sql(client, question, last_ticker=..., recent_turns=...)` (raises `CannotAnswerError` / `GenerationError`).
   b. `validate_sql(sql)` (sqlglot — raises `UnsafeSQLError`).
   c. `execute_safe(db_url_readonly, sql)`.
   d. `log_nl_query(..., path='ai_sql')`. Token counts include the router's tokens plus the generator's.
   e. `_attach_narrative(...)`.

Multi-turn memory bookkeeping in `render_ask_tab`:
- `st.session_state.ask_last_ticker` — single-ticker anchor for follow-ups like "what about volume?"
- `st.session_state.ask_recent_turns` — sliding window of `ConversationTurn` (capped at `MAX_TURNS=3`)
- Both reset by the **Clear history** button.

Adding a new template:
- Append a `QueryTemplate(...)` to `TEMPLATES` in `query_templates.py`.
- Numeric params get `{name}` placeholders (inlined after type+range check).
- String/date params get `%(name)s` placeholders (psycopg-bound).
- Provide 1-3 `nl_examples` — those are the router's primary signal.
- The `_check_template` self-check at module import will refuse to start if any `{placeholder}` or `%(placeholder)s` is undeclared.

Future AI features will reuse `AIClient` and add their own modules under `src/ai/`.

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

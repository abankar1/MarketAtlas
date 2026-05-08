# Market Atlas

### Interactive Market Intelligence Dashboard

## Project Overview

Market Atlas is a data-driven platform that:

- Ingests daily OHLCV stock data for S&P 500, NASDAQ-100, and Dow 30
- Stores it in PostgreSQL with the TimescaleDB extension (time-series optimized)
- Provides an interactive Streamlit dashboard — treemap heatmap, sector breadth, per-stock candlestick charts with technical indicators, per-symbol news with sentiment, and index overlap analysis
- Auto-syncs index constituents monthly from [yfiua/index-constituents](https://github.com/yfiua/index-constituents)
- Supports 10-year historical backfill and incremental daily updates

---

## Prerequisites

- **Python 3.11+**
- **PostgreSQL 14+** with the **TimescaleDB extension** installed
  - macOS: `brew install timescaledb` then follow post-install steps
  - See [docs/timescaledb-cheatsheet.md](docs/timescaledb-cheatsheet.md) for hypertable setup, compression, and retention policies
- A [Marketstack](https://marketstack.com) API access key (`marketdata_token`)
- *(Optional)* A [Marketaux](https://www.marketaux.com) API key (`marketaux_token`) — enables the News tab (free tier: 100 requests/day)
- *(Optional)* An [Anthropic](https://console.anthropic.com) API key (`anthropic_api_key`) — used for AI-based GICS sector classification of new stocks

---

## Setup (First Time)

### 1. Create virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

If you don't have `requirements.txt`:

```bash
pip install psycopg[binary] pandas requests streamlit plotly anthropic
```

### 3. Configure credentials

Copy the example config and fill in your values:

```bash
cp src/config/configuration.json.example src/config/configuration.json
```

Edit `src/config/configuration.json`:

```json
{
  "db_url": "postgresql://user:password@localhost:5432/market_timeseries",
  "marketdata_token": "YOUR_MARKETSTACK_ACCESS_KEY",
  "days": 1000,
  "api_sleep_seconds": 0.2,
  "anthropic_api_key": "YOUR_ANTHROPIC_API_KEY (optional)",
  "marketaux_token": "YOUR_MARKETAUX_API_KEY (optional — enables News tab)"
}
```

> `src/config/configuration.json` is git-ignored and will never be committed.

---

## Database Setup

Ensure TimescaleDB is running and the required tables exist before running any importers.
See [docs/timescaledb-cheatsheet.md](docs/timescaledb-cheatsheet.md) for the full SQL to:
- Create `assets`, `daily_bars`, `sp500_constituents`, `nasdaq100_constituents`, `dow30_constituents`
- Convert `daily_bars` to a hypertable
- Set retention/compression policies

---

## Ask tab setup (one-time)

The Ask tab requires a separate read-only Postgres role and an audit log table.

1. Edit `scripts/setup_readonly_role.sql` and replace `CHANGE_ME_STRONG_PASSWORD`
   with a strong password (also adjust the database name from `market_timeseries`
   if yours differs).
2. Run all three scripts as a DB superuser:
   ```bash
   psql -U postgres -d market_timeseries -f scripts/setup_readonly_role.sql
   psql -U postgres -d market_timeseries -f scripts/create_nl_queries_table.sql
   psql -U postgres -d market_timeseries -f scripts/add_path_to_nl_queries.sql
   ```
3. Add `db_url_readonly` and `anthropic_api_key` to `src/config/configuration.json`
   (and optionally `anthropic_model`, defaults to `claude-haiku-4-5`).
4. Restart the dashboard.

How a question gets answered (hybrid template-first flow):

1. **Intent routing** — Claude classifies the question against a small
   library of pre-approved templates in [src/ai/query_templates.py](src/ai/query_templates.py)
   and emits a JSON intent like `{"template": "sector_count_by_index", "params": {"index": "sp500"}}`.
2. **Template path** — if a template matched, `query_templates.render()`
   validates the params against a typed `ParamSpec` (allowlist for sectors
   / indices, min-max for numerics, regex for symbols), binds string params
   via psycopg, and inlines numeric params after type-checking. The model
   never sees the SQL bodies — its only job is intent classification.
3. **AI-SQL fallback** — if the router decides no template fits
   (`{"template": null}`), the existing free-form generator runs:
   Claude writes a `SELECT`, `sqlglot` validates it (rejects DML/DDL even
   inside CTEs and a deny-list of dangerous functions), and the result
   runs on the readonly role.
4. **Audit log** — every question (template or AI-SQL, success or failure)
   is recorded in `public.nl_queries` with status, row count, duration,
   Anthropic token usage, the path taken (`'template'` vs `'ai_sql'`),
   and (for templates) the matched template name + bound parameters as
   JSONB for replay.

Safety notes:
- All SQL — templated or AI-generated — runs only on the `marketatlas_reader`
  role, which has `SELECT`-only privileges and a role-level
  `statement_timeout = 5s`.
- Adding a new question shape means appending a new `QueryTemplate` to
  [src/ai/query_templates.py](src/ai/query_templates.py); the import-time
  self-check parses every template through sqlglot and aborts startup if
  any one is malformed.

---

## Data Workflow (Order Matters)

Run these steps once on a fresh database, then only the daily update is needed afterward.

### Step 1 — Sync index constituents

Populate the three index membership tables from live data:

```bash
source .venv/bin/activate
python -m src.sync_constituents
```

This fetches the latest S&P 500, NASDAQ-100, and Dow 30 membership from
[yfiua/index-constituents](https://github.com/yfiua/index-constituents) (no API key required),
diffs against the database, and handles additions/removals automatically.

Options:

```bash
python -m src.sync_constituents --dry-run       # show diff without writing
python -m src.sync_constituents --skip-sector   # skip AI sector classification
```

### Step 2 — Backfill historical data (one-time)

Fetch up to 10 years of daily bars for every symbol across all three indices:

```bash
python -m src.backfill.backfill_10y
```

Custom number of years (example: 5 years):

```bash
python -m src.backfill.backfill_10y --years 5
```

### Step 3 — Daily incremental update

Run this each trading day to pull the latest bars:

```bash
python -m src.main
```

This automatically syncs constituents first (skippable with `--skip-sync`),
then fetches only missing price data for all active symbols.

---

## Running the Dashboard

```bash
source .venv/bin/activate
streamlit run src/dashboard/app.py
```

Six tabs, all driven by the same sidebar filters:

- **Heatmap** — treemap tiles sized by dollar volume, colored by **percentile rank** of return % within the visible universe (Worst / Median / Best); collapsible Options panel (View toggle, sector filter, palette); top movers strip; sector parents show dollar-volume-weighted return + avg start/end close on hover
- **Sector Synopsis** — sector breadth bar chart (click to drill in); ranked per-stock bar chart; auto-selects the top-performing sector on load
- **Stock Detail** — full per-stock candlestick with toggleable overlays (SMA 20/50, EMA 20, Bollinger Bands, RSI, MACD, ATR, OBV); index membership badges; Compare mode for normalized multi-symbol performance comparison (up to 5 symbols); defaults to AAPL
- **News** — recent per-symbol headlines from [Marketaux](https://www.marketaux.com) with title, snippet, source, relative timestamp, and sentiment pill (Positive / Neutral / Negative). Symbol selection is shared with Stock Detail. Requires `marketaux_token` in config; otherwise the tab shows a setup notice.
- **Index Overlap** — cross-membership breakdown showing how symbols are distributed across S&P 500, NASDAQ-100, and Dow 30 (exclusive / shared / all three); expandable per-bucket symbol tables
- **Ask** — natural-language query tab. Type a question in plain English ("Which Health Care stocks in the S&P 500 are up more than 10% in the last 30 days?"). Claude first tries to route the question to one of the pre-approved SQL templates in [src/ai/query_templates.py](src/ai/query_templates.py); if no template matches, it falls back to free-form `SELECT` generation. Both paths run on a separate read-only Postgres role with a 5-second statement timeout and a 1,000-row cap. Every question is logged to `nl_queries` with the path taken (`'template'` vs `'ai_sql'`). Requires `db_url_readonly` and `anthropic_api_key` in config; see the **Ask tab setup** section below.

Sidebar controls:
- Index selector: S&P 500 | NASDAQ-100 | Dow 30 | All
- Date range: preset buttons (3M, 6M, 1Y, 2Y, YTD) or custom date pickers
- Cache stats: treemap, OHLCV, and news caches (all session-level with a 12-hour TTL)

Color palette options (Heatmap Options panel): **Finviz-style** (default — dark red → grey → dark green), RdYlGn, RdBu (colorblind-safe), Viridis (sequential).

UI preferences (palette, index, default date preset, indicator selection) are persisted to `~/.marketatlas/prefs.json` and restored across browser sessions.

---

## Constituent Sync

Index membership is kept current via the sync service. It pulls data from
[yfiua/index-constituents](https://github.com/yfiua/index-constituents)
(updated monthly on the 1st, no API key required).

- **Additions** — new symbols are inserted into the constituent table and a minimal `assets` row is created
- **Removals** — dropped symbols are soft-deleted (`is_active = FALSE`, `removed_date` recorded) so historical data is preserved
- **Sector classification** — new symbols are classified into GICS sectors via the Claude API (if `anthropic_api_key` is configured)

The sync runs automatically at the start of `python -m src.main`. To run it standalone:

```bash
python -m src.sync_constituents
```

---

## Backup

Snapshot tables + pg_dump to a custom-format file:

```bash
python -m scripts.backup_db --dump --dump-dir ./backups
```

---

## Quick Module Checks

```bash
python -c "from src.marketdata.client import MarketDataClient; print('client ok')"
python -c "from src.db import repositories as db_repositories; print('repos ok')"
```

---

## Debugging

Always use module mode when running src scripts:

```bash
python -m src.main        # correct
python src/main.py        # incorrect — breaks relative imports
```

---

## Database Tables

| Table | Purpose |
|---|---|
| `assets` | Master symbol registry (name, exchange, GICS sector) |
| `daily_bars` | Time-series OHLCV + adjusted prices, splits, dividends |
| `sp500_constituents` | S&P 500 index membership (with `is_active` soft-delete flag) |
| `nasdaq100_constituents` | NASDAQ-100 membership (with `is_active` soft-delete flag) |
| `dow30_constituents` | Dow 30 membership (with `is_active` soft-delete flag) |

---

## Project Structure

```
Market Atlas/
├── src/
│   ├── config/
│   │   ├── configuration.json          # git-ignored (real credentials)
│   │   ├── configuration.json.example  # committed (safe placeholder)
│   │   └── settings.py
│   ├── db/
│   │   ├── connection.py
│   │   └── repositories.py
│   ├── marketdata/
│   │   ├── client.py                   # Marketstack API client (OHLCV)
│   │   └── news_client.py              # Marketaux API client (news + sentiment)
│   ├── services/
│   │   ├── constituent_sync.py         # yfiua index membership sync
│   │   ├── daily_bar_importer.py       # incremental OHLCV importer
│   │   └── sector_classifier.py        # GICS sector classification (Claude API)
│   ├── dashboard/
│   │   ├── app.py                      # Streamlit entry point — sidebar + nav
│   │   ├── data.py                     # DB queries + LRU caches
│   │   ├── charts.py                   # Plotly figure builders
│   │   ├── indicators.py               # SMA, EMA, Bollinger, RSI, MACD, ATR, OBV
│   │   ├── prefs.py                    # ~/.marketatlas/prefs.json persistence
│   │   ├── heatmap.py                  # Heatmap tab
│   │   ├── sector_synopsis.py          # Sector Synopsis tab
│   │   ├── stock_detail.py             # Stock Detail tab
│   │   ├── news.py                     # News tab (Marketaux headlines)
│   │   └── index_overlap.py            # Index Overlap tab
│   ├── backfill/
│   │   └── backfill_10y.py
│   ├── main.py                         # Daily update orchestrator
│   └── sync_constituents.py            # Constituent sync entry point
├── docs/
│   ├── user-guide.md                   # Dashboard user guide
│   └── timescaledb-cheatsheet.md       # Database setup SQL + reference
└── readme.md
# (scripts/ is gitignored — see .gitignore)
```

---

## Useful Tips

- Always activate `.venv` before running any command
- Use `-m src.*` to avoid import errors
- If data looks stale → rerun `python -m src.main`
- If missing history → run `python -m src.backfill.backfill_10y`
- If index membership is outdated → run `python -m src.sync_constituents`

---

## Upcoming Features

- Natural Language Query Agent (NL → SQL)
- AI-generated market summaries
- Risk monitoring alerts
- Portfolio analysis layer
- Autonomous market intelligence agent

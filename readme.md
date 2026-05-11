# Market Atlas

### AI-Powered Market Intelligence Web App

## Project Overview

Market Atlas is a data-driven platform that:

- Ingests daily OHLCV stock data for S&P 500, NASDAQ-100, and Dow 30
- Stores it in PostgreSQL with the TimescaleDB extension (time-series optimized)
- Provides an interactive Streamlit dashboard — treemap heatmap, sector breadth, per-stock candlestick charts with technical indicators, per-symbol news with sentiment, and index overlap analysis
- Auto-syncs index constituents monthly from [yfiua/index-constituents](https://github.com/yfiua/index-constituents)
- Supports 10-year historical backfill and incremental daily updates

---

## Prerequisites

- **Python 3.12+** (Streamlit Cloud runs 3.12; local works on 3.12-3.14)
- **PostgreSQL 14+** with the **TimescaleDB extension** installed
  - macOS: `brew install timescaledb` then follow post-install steps
  - Or use [Tigerdata Cloud](https://console.cloud.timescale.com) (managed TimescaleDB) — see [docs/deployment.md](docs/deployment.md)
  - See [docs/timescaledb-cheatsheet.md](docs/timescaledb-cheatsheet.md) for hypertable setup, compression, and retention policies
- A [Marketstack](https://marketstack.com) API access key (`marketdata_token`)
- An [Anthropic](https://console.anthropic.com) API key (`anthropic_api_key`) — required for the Ask AI tab; also used for AI-based GICS sector classification
- *(Optional)* A [Marketaux](https://www.marketaux.com) API key (`marketaux_token`) — enables the News tab (free tier: 100 requests/day)

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

`src/config/settings.py` reads config from three sources in priority order:

1. **Streamlit secrets** (Streamlit Community Cloud — paste into *Settings → Secrets*; or `.streamlit/secrets.toml` locally — git-ignored)
2. **Environment variables** (`DB_URL`, `MARKETDATA_TOKEN`, `ANTHROPIC_API_KEY`, …) — used by the GitHub Actions daily-update job
3. **`src/config/configuration.json`** — local-dev fallback (git-ignored)

Local-dev setup:

```bash
cp src/config/configuration.json.example src/config/configuration.json
```

Edit `src/config/configuration.json`:

```json
{
  "db_url": "postgresql://user:password@localhost:5432/market_timeseries",
  "db_url_readonly": "postgresql://atlas_reader:password@localhost:5432/market_timeseries",
  "marketdata_token": "YOUR_MARKETSTACK_ACCESS_KEY",
  "days": 1000,
  "api_sleep_seconds": 0.2,
  "anthropic_api_key": "YOUR_ANTHROPIC_API_KEY",
  "anthropic_model": "claude-haiku-4-5",
  "marketaux_token": "YOUR_MARKETAUX_API_KEY (optional — enables News tab)"
}
```

> `src/config/configuration.json` is git-ignored and will never be committed.
>
> For cloud deployment (Streamlit Cloud + Tigerdata Cloud + GitHub Actions
> daily refresh), follow [docs/deployment.md](docs/deployment.md).

---

## Database Setup

Ensure TimescaleDB is running and the required tables exist before running any importers.
See [docs/timescaledb-cheatsheet.md](docs/timescaledb-cheatsheet.md) for the full SQL to:
- Create `assets`, `daily_bars`, `sp500_constituents`, `nasdaq100_constituents`, `dow30_constituents`
- Convert `daily_bars` to a hypertable
- Set retention/compression policies

---

## Ask AI tab setup (one-time)

The Ask AI tab needs a separate read-only Postgres role plus the `nl_queries`
audit table. For Tigerdata Cloud, see [docs/deployment.md](docs/deployment.md)
(Phase 1). For a local Postgres:

```sql
CREATE ROLE atlas_reader LOGIN PASSWORD 'PICK-A-STRONG-PASSWORD';
GRANT CONNECT ON DATABASE market_timeseries TO atlas_reader;
GRANT USAGE  ON SCHEMA public                TO atlas_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public  TO atlas_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO atlas_reader;
```

Then in `configuration.json`:

```jsonc
"db_url_readonly": "postgresql://atlas_reader:PASSWORD@localhost:5432/market_timeseries",
"anthropic_api_key": "sk-ant-...",
"anthropic_model": "claude-haiku-4-5"   // optional, this is the default
```

The `nl_queries` audit table and the `usage_events` lifecycle table are
auto-created on first use — no manual DDL required.

How a question gets answered (hybrid template-first flow):

1. **Intent routing** — Claude classifies the question against a small
   library of pre-approved templates in [src/ai/query_templates.py](src/ai/query_templates.py)
   and emits a JSON intent like `{"template": "sector_count_by_index", "params": {"index": "sp500"}}`.
   Recent conversational turns are folded into the prompt so referential
   follow-ups like "their names" or "what about volume?" resolve correctly.
2. **Template path** — if a template matched, `query_templates.render()`
   validates the params against a typed `ParamSpec` (allowlist for sectors
   / indices, min-max for numerics, regex for symbols), binds string params
   via psycopg, and inlines numeric params after type-checking. The model
   never sees the SQL bodies — its only job is intent classification.
3. **AI-SQL fallback** — if the router decides no template fits, the
   free-form generator runs: Claude writes a `SELECT`, `sqlglot` validates
   it (rejects DML/DDL even inside CTEs and a deny-list of dangerous
   functions), and the result runs on the readonly role.
4. **Narrative summary** — a short Haiku call turns the result rows into a
   1-line answer in plain English. Empty rows ("No rows matched.") and a
   "See the **Stock Detail** tab for the full chart." pointer are handled
   here.
5. **Audit log** — every question is logged to `public.nl_queries` with
   status, row count, duration, token usage, path taken, template name +
   bound params (template path), `from_cache`, and `raw_response`.

Safety notes:
- All SQL — templated or AI-generated — runs only on the `atlas_reader`
  role with `SELECT`-only privileges. `execute_safe()` sets a
  per-connection `statement_timeout=15000ms` and `SET TRANSACTION READ ONLY`.
- Adding a new question shape means appending a new `QueryTemplate` to
  [src/ai/query_templates.py](src/ai/query_templates.py); the import-time
  self-check parses every template through sqlglot and aborts startup if
  any one is malformed.
- The LLM decision caches (route / ai_sql / narrate) are versioned via
  `PROMPT_VERSION` in [src/ai/cache.py](src/ai/cache.py) — bumping the
  string invalidates every stale entry on the next deploy.

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

### Locally

```bash
source .venv/bin/activate
streamlit run src/dashboard/app.py
```

### Hosted

A managed deployment stack is documented in [docs/deployment.md](docs/deployment.md):

- **Streamlit Community Cloud** serves the dashboard from the `release` branch
- **Tigerdata Cloud** (managed TimescaleDB) holds the data
- **GitHub Actions** (`.github/workflows/daily-update.yml`) runs `python -m src.main` on a cron schedule (Mon/Wed/Fri 09:00 UTC) so the cloud DB stays current

Five tabs, all driven by the same sidebar filters:

- **Heatmap** — treemap tiles sized by dollar volume, colored by **percentile rank** of return % within the visible universe (Worst / Median / Best); collapsible Options panel (View toggle, sector filter, palette); top-5 movers strip (▲/▼); sector parents show dollar-volume-weighted return + avg start/end close on hover
- **Ask AI** — natural-language query tab. Type a question in plain English ("Which Health Care stocks in the S&P 500 are up more than 10% in the last 30 days?"). Claude first tries to route the question to one of the pre-approved SQL templates in [src/ai/query_templates.py](src/ai/query_templates.py); if no template matches, it falls back to free-form `SELECT` generation. Multi-turn memory carries the last few turns so follow-ups like "their names" or "what about volume?" resolve. Both paths run on a separate read-only Postgres role with a 15-second statement timeout and a 1,000-row cap. Every question is logged to `nl_queries`. Requires `db_url_readonly` and `anthropic_api_key` in config; see the **Ask AI tab setup** section below.
- **Sector Synopsis** — sector breadth bar chart (click to drill in); ranked per-stock bar chart; auto-selects the top-performing sector on load
- **Stock Detail** — full per-stock candlestick with toggleable overlays (SMA 20/50, EMA 20, Bollinger Bands, RSI, MACD, ATR, OBV); index membership badges; Compare mode for normalized multi-symbol performance comparison (up to 5 symbols); defaults to AAPL
- **News** — recent per-symbol headlines from [Marketaux](https://www.marketaux.com) with title, snippet, source, relative timestamp, and sentiment pill (Positive / Neutral / Negative). Symbol selection is shared with Stock Detail. Requires `marketaux_token` in config; otherwise the tab shows a setup notice.

> An **Index Overlap** view exists in the codebase (`src/dashboard/index_overlap.py`) but is currently hidden from the tab strip. Re-enable it by adding `"Index Overlap"` back into `_TABS` in [src/dashboard/app.py](src/dashboard/app.py).

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

For local Postgres, `pg_dump` is the standard route. For Tigerdata Cloud,
see the migration recipe in [docs/deployment.md](docs/deployment.md) Phase 2
(includes the `timescaledb_pre_restore()` / `timescaledb_post_restore()`
incantation needed when restoring TimescaleDB hypertables).

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
│   │   ├── configuration.json          # git-ignored (real credentials, local-dev fallback)
│   │   ├── configuration.json.example  # committed (safe placeholder)
│   │   └── settings.py                 # 3-source loader: st.secrets → env vars → JSON file
│   ├── db/
│   │   ├── connection.py
│   │   ├── readonly.py                 # sqlglot validator + readonly executor (Ask AI)
│   │   └── repositories.py             # all SQL — upserts, fetches, nl_queries + usage_events log
│   ├── ai/
│   │   ├── client.py                   # Anthropic Messages API wrapper (httpx)
│   │   ├── query_templates.py          # pre-approved SQL templates (Ask AI primary path)
│   │   ├── intent_router.py            # Claude call: question → template name + params
│   │   ├── nl_to_sql.py                # free-form NL → SQL fallback
│   │   ├── narrate.py                  # 1-line conversational summary of a SQL result
│   │   ├── memory.py                   # ConversationTurn + transcript helpers (multi-turn memory)
│   │   └── cache.py                    # process-wide LRU+TTL caches; PROMPT_VERSION invalidation
│   ├── marketdata/
│   │   ├── client.py                   # Marketstack API client (OHLCV)
│   │   └── news_client.py              # Marketaux API client (news + sentiment)
│   ├── services/
│   │   ├── constituent_sync.py         # yfiua index membership sync
│   │   ├── daily_bar_importer.py       # incremental OHLCV importer
│   │   └── sector_classifier.py        # GICS sector classification (Claude API)
│   ├── dashboard/
│   │   ├── app.py                      # Streamlit entry point — sidebar, nav, lifecycle logging
│   │   ├── data.py                     # DB queries + LRU caches
│   │   ├── charts.py                   # Plotly figure builders
│   │   ├── indicators.py               # SMA, EMA, Bollinger, RSI, MACD, ATR, OBV
│   │   ├── prefs.py                    # ~/.marketatlas/prefs.json persistence
│   │   ├── heatmap.py                  # Heatmap tab + top-5 movers strip
│   │   ├── sector_synopsis.py          # Sector Synopsis tab
│   │   ├── stock_detail.py             # Stock Detail tab
│   │   ├── news.py                     # News tab (Marketaux headlines)
│   │   ├── index_overlap.py            # Index Overlap tab (hidden — see _TABS)
│   │   └── ask.py                      # Ask AI tab (router → template OR AI-SQL fallback)
│   ├── backfill/
│   │   └── backfill_10y.py
│   ├── main.py                         # Daily update orchestrator
│   └── sync_constituents.py            # Constituent sync entry point
├── docs/
│   ├── user-guide.md                   # Dashboard user guide
│   ├── deployment.md                   # Streamlit Cloud + Tigerdata Cloud + GHA runbook
│   └── timescaledb-cheatsheet.md       # Database setup SQL + reference
├── .github/workflows/
│   └── daily-update.yml                # GHA cron: market data refresh (Mon/Wed/Fri 09:00 UTC)
├── .streamlit/
│   ├── config.toml                     # toolbarMode = "minimal"
│   └── secrets.toml.example            # committed template for Streamlit Cloud secrets UI
├── AGENTS.md                           # AI-agent-facing architecture doc
└── readme.md
# (scripts/ is gitignored — operator-only files)
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

- AI-generated end-of-day market summaries
- Risk monitoring alerts
- Portfolio analysis layer
- Autonomous market intelligence agent

> Already shipped: the Ask AI natural-language query tab (Claude Haiku router →
> pre-approved templates with AI-SQL fallback, multi-turn memory, narrative
> summary, readonly-role execution, full audit log in `nl_queries`).

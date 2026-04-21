# Market Atlas

### Interactive Market Intelligence Dashboard

## Project Overview

Market Atlas is a data-driven platform that:

- Ingests daily OHLCV stock data for S&P 500, NASDAQ-100, and Dow 30
- Stores it in PostgreSQL with the TimescaleDB extension (time-series optimized)
- Provides an interactive Streamlit dashboard — treemap heatmap + per-stock candlestick charts with technical indicators
- Auto-syncs index constituents monthly from [yfiua/index-constituents](https://github.com/yfiua/index-constituents)
- Supports 10-year historical backfill and incremental daily updates

---

## Prerequisites

- **Python 3.11+**
- **PostgreSQL 14+** with the **TimescaleDB extension** installed
  - macOS: `brew install timescaledb` then follow post-install steps
  - See `TimescaleDbCheatsheet.txt` for hypertable setup, compression, and retention policies
- A [Marketstack](https://marketstack.com) API access key (`marketdata_token`)
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
  "anthropic_api_key": "YOUR_ANTHROPIC_API_KEY (optional)"
}
```

> `src/config/configuration.json` is git-ignored and will never be committed.

---

## Database Setup

Ensure TimescaleDB is running and the required tables exist before running any importers.
See `TimescaleDbCheatsheet.txt` for the full SQL to:
- Create `assets`, `daily_bars`, `sp500_constituents`, `nasdaq100_constituents`, `dow30_constituents`
- Convert `daily_bars` to a hypertable
- Set retention/compression policies

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

Features:
- **Heatmap** — treemap tiles sized by dollar volume, colored by return % (green gain / red loss)
- **Stock Detail** — per-stock candlestick chart with toggleable overlays (SMA 20/50, EMA 20, Bollinger Bands, RSI 14)
- Index selector: S&P 500 | NASDAQ-100 | Dow 30 | All
- Date range picker with presets (3m, 6m, 1y, 2y, YTD) or custom dates
- KPIs: median return, best/worst performer, symbol count
- In-memory LRU cache to avoid repeated DB queries

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
│   │   └── client.py                   # Marketstack API client
│   ├── services/
│   │   ├── constituent_sync.py         # yfiua index membership sync
│   │   ├── daily_bar_importer.py       # incremental OHLCV importer
│   │   └── sector_classifier.py        # GICS sector classification (Claude API)
│   ├── dashboard/
│   │   ├── app.py                      # Streamlit UI (heatmap + stock detail)
│   │   └── indicators.py              # SMA, EMA, RSI, Bollinger Bands
│   ├── backfill/
│   │   └── backfill_10y.py
│   ├── main.py                         # Daily update orchestrator
│   └── sync_constituents.py           # Constituent sync entry point
├── scripts/
│   ├── backup_db.py                    # pg_dump + snapshot utility
│   └── migrate_add_sector.py          # One-time GICS sector migration
├── TimescaleDbCheatsheet.txt           # Database setup SQL + reference
└── readme.md
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

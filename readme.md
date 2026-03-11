# Market Atlas

### Interactive Market Intelligence Dashboard

## Project Overview

Market Atlas is a data-driven platform that:

- Ingests daily OHLCV stock data for S&P 500, NASDAQ-100, and Dow 30
- Stores it in PostgreSQL with the TimescaleDB extension (time-series optimized)
- Provides an interactive Streamlit dashboard — treemap heatmap colored by return %
- Supports 10-year historical backfill and incremental daily updates

---

## Prerequisites

- **Python 3.11+**
- **PostgreSQL 14+** with the **TimescaleDB extension** installed
  - macOS: `brew install timescaledb` then follow post-install steps
  - See `timescaledb_basic_commands.txt` for hypertable setup, compression, and retention policies
- A [Marketstack](https://marketstack.com) API access key (`marketdata_token`)

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
pip install psycopg[binary] pandas requests streamlit plotly
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
  "api_sleep_seconds": 0.2
}
```

> `src/config/configuration.json` is git-ignored and will never be committed.

---

## Database Setup

Ensure TimescaleDB is running and the required tables exist before running any importers.
See `timescaledb_basic_commands.txt` for the full SQL to:
- Create `assets`, `daily_bars`, `sp500_constituents`, `nasdaq100_constituents`, `dow30_constituents`
- Convert `daily_bars` to a hypertable
- Set retention/compression policies

---

## Data Workflow (Order Matters)

Run these steps once on a fresh database, then only the daily update is needed afterward.

### Step 1 — Load index constituents

Populate the three index membership tables (who belongs to which index):

```bash
source .venv/bin/activate

# S&P 500 (reads from snp500.csv)
python snpDataImport.py

# NASDAQ-100 (reads from data/nasdaq100.tsv)
python -m src.load_nasdaq

# Dow 30 (reads from data/dow30.tsv)
python -m src.load_dow
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

This fetches only missing data (incremental — won't re-download history).

---

## Running the Dashboard

```bash
source .venv/bin/activate
streamlit run src/dashboard/app.py
```

Features:
- Index selector: S&P 500 | NASDAQ-100 | Dow 30 | All
- Date range picker with presets (3m, 6m, 1y, 2y, YTD) or custom dates
- Treemap tiles sized by dollar volume, colored by return % (green gain / red loss)
- KPIs: median return, best/worst performer, symbol count
- In-memory LRU cache to avoid repeated DB queries

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

VSCode launch configurations are in `.vscode/launch.json`.

---

## Database Tables

| Table | Purpose |
|---|---|
| `assets` | Master symbol registry (name, exchange, asset type) |
| `daily_bars` | Time-series OHLCV + adjusted prices, splits, dividends |
| `sp500_constituents` | S&P 500 index membership with GICS sector/sub-industry |
| `nasdaq100_constituents` | NASDAQ-100 membership with ICB industry classification |
| `dow30_constituents` | Dow 30 membership with index weighting |

---

## Project Structure

```
TimeScaleDB Project/
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
│   │   ├── daily_bar_importer.py
│   │   ├── nasdaq_loader.py
│   │   └── dow_loader.py
│   ├── dashboard/
│   │   └── app.py                      # Streamlit UI
│   ├── backfill/
│   │   └── backfill_10y.py
│   ├── main.py                         # Daily update orchestrator
│   ├── load_nasdaq.py
│   └── load_dow.py
├── scripts/
│   └── backup_db.py
├── data/
│   ├── nasdaq100.tsv                   # NASDAQ-100 constituent list
│   └── dow30.tsv                       # Dow 30 constituent list
├── snpDataImport.py                    # S&P 500 constituent loader
├── snp500.csv                          # S&P 500 constituent data
├── timescaledb_basic_commands.txt      # TimescaleDB SQL reference
└── .venv/                              # git-ignored virtual environment
```

---

## Useful Tips

- Always activate `.venv` before running any command
- Use `-m src.*` to avoid import errors
- If data looks stale → rerun `python -m src.main`
- If missing history → run `python -m src.backfill.backfill_10y`

---

## Upcoming Features

- Natural Language Query Agent (NL → SQL)
- AI-generated market summaries
- Risk monitoring alerts
- Portfolio analysis layer
- Autonomous market intelligence agent

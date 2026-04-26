# MarketAtlas — User Guide

MarketAtlas is a market intelligence dashboard for tracking price performance
across the S&P 500, NASDAQ-100, and Dow 30. It pulls historical and daily price
data from Marketstack, stores it in a local TimescaleDB database, and surfaces
it through an interactive Streamlit dashboard.

---

## What You Can Do

- See which sectors and stocks are up or down over any date range
- Drill into a specific sector to compare all its stocks side by side
- Pull up a candlestick chart for any individual stock with technical overlays
- Keep your constituent lists automatically up to date as stocks are added or removed from indices
- Run a daily update that fetches the latest prices for everything in your database

---

## Dashboard

Start the dashboard with:

```bash
streamlit run src/dashboard/app.py
```

It opens at `http://localhost:8501`.

---

### Sidebar Controls

Every filter applies to all three tabs simultaneously.

**Index universe**
Choose which index to look at: S&P 500, NASDAQ-100, Dow 30, or All (combined, deduplicated). Defaults to All.

**Quick range**
Pick a preset time window: Past 3 months (default), Past 6 months, Past 1 year, Past 2 years, or YTD. If you manually adjust the date pickers after choosing a preset, the preset highlight clears to indicate a custom range.

**Start date / End date**
The date pickers are bounded to the earliest and latest bar dates actually in your database for the selected universe — you can't accidentally select a range with no data.

**Color scaling**
The heatmap colors everything on a Red-Yellow-Green scale. This slider (default ±10%) sets the clip range. Stocks with returns larger than the ceiling or smaller than the floor still appear, but are rendered at the color boundary — so one extreme outlier doesn't wash out everything else.

**Cache**
Shows how many query results are cached for your current session (entries, hits, misses). Hit "Clear cached results" if you want to force fresh queries from the database.

---

### Heatmap Tab

The main view. Every stock in the selected universe appears as a rectangle sized by its dollar volume (end price × shares traded) and colored by its return % over the selected date range.

- Rectangles are grouped by GICS sector. Click a sector group to zoom in on just that sector. Click the breadcrumb to zoom back out.
- Hover over any tile to see: return %, start close, end close, dollar volume, and which index it belongs to.
- Stocks with no price data in the selected range are not shown.
- Stocks without a sector classification appear under "Unknown".

**Show raw data**
An expander below the chart shows the full dataset as a table, sorted by return % descending. Note: opening this expander refreshes the page, which resets any sector drill-down you'd navigated into on the treemap.

**Export CSV**
An "⬇ Export CSV" button sits next to the view toggle. It downloads symbol, name, sector, return %, dollar volume, and percentile rank for every stock in the current universe and date range. The button is disabled when the selected range exceeds 3 calendar months — narrow the range to enable it.

---

### Sector Synopsis Tab

A focused breakdown of one sector at a time.

**How to use it:**
1. Pick a sector from the dropdown at the top.
2. Read the KPI row: number of stocks, median return, average return, gainers vs losers count, and total dollar volume for the sector.
3. Read the auto-generated text summary below.
4. Look at the ranked bar chart — stocks sorted best to worst, green for positive, red for negative. The dashed yellow line marks the sector average.

**Clicking a bar:**
Click any bar to expand an inline detail panel below the chart. It shows the stock's return %, start price, end price, dollar volume, and a candlestick chart for the selected date range. Click a different bar to switch to that stock. The sector dropdown stays active — you haven't left the tab.

**Show raw data**
An expander at the bottom of the synopsis shows a formatted table with symbol, name, return %, start/end close, and dollar volume for all stocks in the sector.

---

### Stock Detail Tab

A full per-stock technical chart with configurable indicator panels.

**How to use it:**
1. Type in the search box to filter the symbol list (searches by ticker, company name, or sector).
2. Select a symbol from the dropdown. Each entry shows the sector and return % for context.
3. Colored index membership badges appear below the selector — blue for S&P 500, purple for NASDAQ-100, teal for Dow 30.
4. Choose which overlays to display using the multiselect (defaults: SMA 20, SMA 50).

**The chart layout is dynamic.** Two panels are always shown; additional panels are added for each sub-panel indicator you enable:
- **Top (largest):** Candlestick price chart with any active price overlays (SMA, EMA, Bollinger Bands).
- **Volume:** Bar chart, colored green or red to match the corresponding candle.
- **RSI** *(if enabled)*: 14-period Relative Strength Index. Dotted red line at 70 (overbought), dotted green line at 30 (oversold).
- **MACD** *(if enabled)*: 12/26/9 MACD. Green/red histogram bars + blue MACD line + orange dotted signal line.
- **ATR** *(if enabled)*: 14-period Average True Range. Purple filled area — higher = more volatile.
- **OBV** *(if enabled)*: On-Balance Volume. Cyan line — rising OBV confirms price trends.

**Available overlays:**
| Overlay | Panel | Description | Min bars |
|---------|-------|-------------|----------|
| SMA 20 | Price | 20-day Simple Moving Average (blue) | 20 |
| SMA 50 | Price | 50-day Simple Moving Average (orange) | 50 |
| EMA 20 | Price | 20-day Exponential Moving Average (purple dotted) | 20 |
| Bollinger Bands | Price | 20-period ±2 std dev bands (grey, shaded fill) | 21 |
| RSI | Sub-panel | 14-period Relative Strength Index (0–100) | 15 |
| MACD | Sub-panel | 12/26/9 MACD line + signal + histogram | 35 |
| ATR | Sub-panel | 14-period Average True Range | 15 |
| OBV | Sub-panel | On-Balance Volume (cumulative signed volume) | 2 |

A warning appears if the selected date range is too short for any of the specific indicators you have enabled — it lists only the ones that are affected, so you know exactly what to fix.

**Export CSV**
An "⬇ Export CSV" button above the chart downloads the raw OHLCV bars (date, open, high, low, close, volume) for the selected symbol and date range. Disabled for ranges over 3 calendar months.

---

### Index Overlap Tab

Shows how symbols are distributed across S&P 500, NASDAQ-100, and Dow 30 simultaneously.

**How to read it:**
- **Headline metrics** — total unique symbols across all indices, and the count for each individual index.
- **Cross-membership bar chart** — each bar represents one of seven mutual-exclusion buckets. A symbol appears in exactly one bucket: whichever combination of indices it currently belongs to. For example, a stock in both S&P 500 and NASDAQ-100 but not Dow 30 falls into "S&P 500 + NASDAQ-100".
- **Symbol tables** — expandable per-bucket table with symbol, name, and sector. Click any expander to browse or search the stocks in that bucket.

This tab is most useful when you're working in the "All" universe and want context on how many symbols are shared vs. exclusive.

---

## Keeping Data Fresh

### Daily price update

Run this once a day (or set up a cron job) to fetch the latest bars for all active symbols and sync constituent changes:

```bash
python -m src.main
```

This does two things in order:
1. Syncs the S&P 500, NASDAQ-100, and Dow 30 constituent lists against the latest data from yfiua's GitHub Pages. If any stocks have been added to or removed from an index, the database is updated.
2. Fetches missing price bars for every active symbol. Only the gap since the last stored bar is fetched — it doesn't re-download data you already have.

To skip the constituent sync and only update prices:
```bash
python -m src.main --skip-sync
```

---

### Syncing constituents manually

If you want to check or apply constituent changes without updating prices:

```bash
# See what would change without touching the database
python -m src.sync_constituents --dry-run

# Apply the changes
python -m src.sync_constituents
```

When stocks are removed from an index, they are soft-deleted — they stay in the database but are marked inactive and no longer appear in the dashboard. If a stock rejoins an index later, it is reactivated.

---

### Historical backfill

If you're setting up a fresh database, run the backfill to load up to 10 years of history before starting daily updates:

```bash
python -m src.backfill.backfill_10y
```

For a shorter window:
```bash
python -m src.backfill.backfill_10y --years 3
```

The backfill is safe to re-run — it checks what's already in the database and only fetches what's missing.

---

## Managing Sector Classifications

Every stock in the dashboard is tagged with a GICS sector (e.g., Information Technology, Financials). These drive the grouping in the heatmap and the sector selectbox in the Synopsis tab.

Sector mappings live in `data/gics_sectors.json`. This is a simple file:
```json
{
  "AAPL": "Information Technology",
  "JPM": "Financials",
  ...
}
```

**To check if any stocks are missing a sector:**
```bash
python -m src.load_sectors --check
```

**To apply the file to the database:**
```bash
python -m src.load_sectors
```

**To add a sector for a new stock:** Open `data/gics_sectors.json`, add the entry, then run `python -m src.load_sectors`.

**To export the current database sectors back to the file** (useful if you've classified stocks directly in the DB):
```bash
python -m src.load_sectors --export
```

Valid GICS sectors:
- Communication Services
- Consumer Discretionary
- Consumer Staples
- Energy
- Financials
- Health Care
- Industrials
- Information Technology
- Materials
- Real Estate
- Utilities

Stocks without a classification show under "Unknown" in the heatmap.

---

## Things to Know

**The heatmap resets when you change filters.** Any drill-down navigation you've done in the treemap (clicking into a sector) is stored only in the browser. Changing a sidebar filter or opening the raw data expander triggers a page refresh and resets the view to the top level.

**Tab selection is preserved across filter changes.** Changing the date range or index while on the Sector Synopsis or Stock Detail tab does not send you back to Heatmap. The active tab is stored in session state and survives any sidebar interaction.

**The Sector Synopsis tab does not reset when you click bars.** Bar selection is handled as a partial update — the sector dropdown and tab selection are unaffected.

**The Stock Detail tab remembers your symbol.** If you change the date range or index while viewing a stock, the same stock remains selected when the chart reloads (as long as it still appears in the universe).

**CSV export is limited to 3-month ranges.** The download buttons on the Heatmap and Stock Detail tabs are disabled when the selected date range is more than 3 calendar months. The check is calendar-aware — Jan 22 to Apr 22 is allowed; Jan 22 to Apr 23 is not.

**Date range bounds depend on what's in your database.** The dashboard does not fetch live prices. If you haven't run the daily update recently, the most recent date available will be the last time you ran `python -m src.main`.

**Stocks appearing in multiple indices** (e.g., Apple is in both S&P 500 and NASDAQ-100) appear once in "All" mode.

**Cache is per browser session.** If you open a second browser tab, it starts its own cache. The "Clear cached results" button only clears the cache for the current tab.

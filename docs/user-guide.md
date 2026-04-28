# Market Atlas — User Guide

Market Atlas is a market intelligence dashboard for tracking price performance
across the S&P 500, NASDAQ-100, and Dow 30. It pulls historical and daily price
data from Marketstack, stores it in a local TimescaleDB database, and surfaces
it through an interactive Streamlit dashboard.

---

## What You Can Do

- See which sectors and stocks are up or down over any date range
- Drill into a specific sector to compare all its stocks side by side
- Pull up a candlestick chart for any individual stock with technical overlays
- Compare up to 5 stocks on a normalised performance chart
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

**Index universe**
Choose which index to look at: S&P 500, NASDAQ-100, Dow 30, or All (combined, deduplicated). Defaults to All.

**Quick range**
Pick a preset time window: Past 3 months (default), Past 6 months, Past 1 year, Past 2 years, or YTD. If you manually adjust the date pickers after choosing a preset, the preset highlight clears to indicate a custom range.

**Start date / End date**
The date pickers are bounded to the earliest and latest bar dates actually in your database for the selected universe.

**Size tiles by**
Controls how treemap tile area is calculated:
- *Dollar volume* (default) — tile area ∝ end price × shares traded; larger companies dominate visually
- *Equal weight* — every tile the same size; colour is the only signal
- *Magnitude* — tile area ∝ |return %|; highlights the biggest movers regardless of liquidity

**Cache**
Shows how many query results are cached in the current session (hits, misses, slots used).

UI preferences (index, palette, tile sizing, date preset, indicator selection) are automatically saved to `~/.marketatlas/prefs.json` and restored the next time you open the dashboard.

---

### Heatmap Tab

The main view. Every stock in the selected universe appears as a rectangle colored by its return % over the selected date range.

**Top movers strip**
Above the treemap, a compact strip shows the top 5 gainers (▲) and top 5 losers (▼) for a quick read on the extremes.

**Options panel**
Click the **Options** arrow to expand display controls:
- *View* — switch between Treemap and Ranked Table
- *Sectors* — filter to one or more GICS sectors (empty = all sectors)
- *Clip ±%* — sets the color scale range (default ±10%). Returns beyond the clip are clamped to the boundary colour so a single outlier doesn't wash out everything else. The current value shows as ±N% on the slider.
- *Color palette* — RdYlGn (default), RdBu (colorblind-safe), or Viridis (sequential)
- *Center on 0%* — ON: neutral colour at 0% (green = gain, red = loss). OFF: neutral colour at the period median, highlighting relative out/under-performers on broadly trending days.

**Treemap**
Rectangles are grouped by GICS sector. Hover for return %, start/end close, dollar volume, and index membership.

**Ranked Table**
Sortable table with all symbols ranked by return %, with background-coloured return cells matching the treemap palette, and a percentile bar column.

---

### Sector Synopsis Tab

A focused breakdown of one sector at a time. On first load, the top-performing sector (by average return) is pre-selected.

**Sector Breadth bar chart**
Shows what percentage of stocks in each sector have a positive return. Click any bar to jump directly to that sector in the dropdown below.

**Sector dropdown**
Pick any sector manually. Overrides a breadth bar click.

**Per-sector view**
- KPI row: stock count, median return, average return, gainers vs losers, total dollar volume
- Ranked horizontal bar chart of all stocks in the sector — green for positive, red for negative; dashed yellow line marks the sector average
- Hover any bar for full company name, return %, start/end close, and dollar volume

---

### Stock Detail Tab

A full per-stock technical chart with configurable indicator panels. Defaults to AAPL on first load.

**Candlestick mode**

1. Type in the search box to filter by ticker, company name, or sector.
2. Select a symbol — each entry shows sector and return % for context.
3. Index membership badges appear below the selector (blue = S&P 500, purple = NASDAQ-100, teal = Dow 30).
4. Choose overlays from the multiselect (defaults: SMA 20, SMA 50).

The chart layout is dynamic — two panels are always shown; additional panels are added for each sub-panel indicator:

- **Price (top):** Candlestick + any active price overlays (SMA, EMA, Bollinger Bands)
- **Volume:** Green/red bars matching the candle direction
- **RSI** *(if enabled)*: 14-period RSI; dotted red at 70 (overbought), dotted green at 30 (oversold)
- **MACD** *(if enabled)*: 12/26/9 — histogram + MACD line + signal line
- **ATR** *(if enabled)*: 14-period Average True Range (purple filled area)
- **OBV** *(if enabled)*: On-Balance Volume (cyan line)

| Overlay | Panel | Min bars |
|---------|-------|----------|
| SMA 20 | Price | 20 |
| SMA 50 | Price | 50 |
| EMA 20 | Price | 20 |
| Bollinger Bands | Price | 21 |
| RSI | Sub-panel | 15 |
| MACD | Sub-panel | 35 |
| ATR | Sub-panel | 15 |
| OBV | Sub-panel | 2 |

A warning appears if the date range is too short for any enabled indicator.

**Compare mode**

Switch to Compare using the mode toggle at the top. A single multiselect replaces the symbol picker — add up to 5 symbols directly. All series are rebased to 100 at the first bar so they start at the same point. The first selected symbol is highlighted in yellow.

---

### Index Overlap Tab

Shows how symbols are distributed across S&P 500, NASDAQ-100, and Dow 30.

- **Headline metrics** — total unique symbols, and the count per index
- **Cross-membership bar chart** — seven mutual-exclusion buckets (e.g. "S&P 500 + NASDAQ-100" for stocks in both but not Dow 30)
- **Symbol tables** — expandable per-bucket table with symbol, name, and sector

Most useful when working in the "All" universe.

---

## Keeping Data Fresh

### Daily price update

```bash
python -m src.main
```

Syncs constituents first, then fetches missing bars for all active symbols.

To skip the constituent sync:
```bash
python -m src.main --skip-sync
```

### Syncing constituents manually

```bash
python -m src.sync_constituents --dry-run   # preview changes
python -m src.sync_constituents             # apply changes
```

### Historical backfill

```bash
python -m src.backfill.backfill_10y          # up to 10 years
python -m src.backfill.backfill_10y --years 3
```

The backfill is safe to re-run — it only fetches what is missing.

---

## Things to Know

**Tab selection is preserved across filter changes.** Changing the date range or index while on any tab does not send you back to Heatmap.

**The Stock Detail tab remembers your symbol.** If you change the date range or index while viewing a stock, the same stock stays selected (as long as it exists in the new universe).

**Sector Synopsis defaults to the best-performing sector.** On first load (or after a session reset), the tab auto-selects the sector with the highest mean return for the current filters.

**Date range bounds depend on what's in your database.** The dashboard does not fetch live prices. If you haven't run the daily update recently, the most recent available date will reflect the last run of `python -m src.main`.

**Stocks appearing in multiple indices** (e.g. Apple is in both S&P 500 and NASDAQ-100) appear once in "All" mode and are counted in their respective bucket in the Index Overlap tab.

**Cache is per browser session.** Opening a second browser tab starts a fresh cache.

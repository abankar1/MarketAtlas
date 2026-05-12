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
- Read recent per-symbol news headlines with sentiment scoring
- **Ask plain-English questions** ("top 10 NASDAQ-100 movers this week", "how is NVDA doing?") and get an answer plus the data
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

**Cache**
Shows how many query results are cached in the current session (hits, misses, slots used) for the treemap, OHLCV, and news caches. All three use a shared 12-hour TTL — long enough to keep API/DB usage low (Marketaux free tier is 100 requests/day), short enough that a dashboard left open overnight refreshes data on first use.

UI preferences (index, palette, date preset, indicator selection) are automatically saved to `~/.marketatlas/prefs.json` and restored the next time you open the dashboard.

---

### Heatmap Tab

The main view. Every stock in the selected universe appears as a rectangle sized by its dollar volume. Tiles are colored by **percentile rank** of return % within the visible universe — 0 = worst performer, 100 = best — so a single outlier (e.g. a +300% mover) no longer washes everything else into the same boundary colour. The colorbar is labelled **Worst / Median / Best** rather than raw percent values.

**Top movers strip**
Above the treemap, a compact strip shows the top 5 gainers (▲) and top 5 losers (▼) for a quick read on the extremes.

**Options panel**
Click the **Options** arrow to expand display controls:
- *View* — switch between Treemap and Ranked Table
- *Sectors* — filter to one or more GICS sectors (empty = all sectors)
- *Color palette* — Finviz-style (default — dark red → grey → dark green, no yellow), RdYlGn, RdBu (colorblind-safe), or Viridis (sequential)

**Treemap**
Rectangles are grouped by GICS sector. Hover a stock for return %, percentile, start/end close, and dollar volume. Hover a sector parent for the dollar-volume-weighted return and average start/end close, plus the stock count and total dollar volume for the sector.

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

### News Tab

Recent headlines for any single ticker, fetched from [Marketaux](https://www.marketaux.com).

**Symbol selector**
Pick any ticker in the current universe. The selection is shared with the Stock Detail tab — if you pick AAPL on Stock Detail and switch to News, AAPL is preselected (and vice versa). On a fresh session it defaults to AAPL.

**Headline cards**
Each card shows:
- Article title (clickable, opens in a new tab)
- Sentiment pill — **Positive** (green), **Negative** (red), or **Neutral** (grey), driven by Marketaux's per-entity sentiment score
- A short description snippet (truncated at a word boundary)
- Source · relative timestamp ("3h ago", "2d ago", etc.)

Up to 10 cards per symbol.

**Configuration**
The News tab needs a free [Marketaux](https://www.marketaux.com) API key set as `marketaux_token` in `src/config/configuration.json`. Without it, the tab shows a one-line setup notice and the rest of the dashboard keeps working.

Marketaux's free tier is 100 requests/day. Headlines are cached per-symbol for 12 hours, so revisiting the same ticker doesn't burn quota.

---

### Ask AI Tab

Ask questions in plain English and get an answer plus the data. Example:
*"Which Health Care stocks in the S&P 500 are up more than 10% in the last 30 days?"*

**Scope banner**
A short caption at the very top of the tab sums up what's available — a decade of daily price + volume for the S&P 500, NASDAQ-100, and Dow 30 (~600 stocks). The AI will refuse out-of-scope questions like fundamentals (P/E, market cap), forecasts, news, sentiment, options, and intraday data — those simply aren't in the database.

**Example chips**
Pre-canned questions cover the most common shapes. Desktop sees six full-sentence prompts in two columns (sector returns, single-stock volume, top movers, sector performance, daily-volume spikes, stocks down sharply). Mobile gets a tighter set of four shorter labels in a single stack so the chips don't eat the viewport. Tap one to run it immediately.

**Chat input**
Type any question and press Enter. The submission UI is chat-style:

1. The instant you submit, your question card appears at the bottom of the conversation with an animated "Thinking…" indicator and the page glides down to land on it.
2. When the answer is ready, the indicator is replaced in place by the actual result and the page glides again so the new Q/A sits flush above the chat input.

Each answer card includes:
- A 1-line plain-English summary at the top
- The result table (sortable, capped at 1,000 rows)
- A subtle "instant response" hint when the AI decision came from cache (no new API call)
- A row count and duration in milliseconds

**Multi-turn memory**
The last few turns are carried into each new question, so follow-ups work:

- *"Show me the top 5 NASDAQ-100 movers this week"* → returns a list
- *"Now show their average volume"* → resolves "their" to the symbols just listed
- *"What about over 90 days?"* → keeps the same symbols, swaps the window

Click **Clear history** to drop the memory and start fresh. On mobile the Clear-history button is sized to match the example chips so it doesn't dominate the screen.

**Configuration**
Requires `anthropic_api_key` (Claude API) and `db_url_readonly` (a Postgres role with SELECT-only privileges) in the config. The Ask AI tab can't write to the database — its SQL is validated and runs with a 15-second timeout on a read-only connection.

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

**Cache is per browser session.** Opening a second browser tab starts a fresh cache. All caches (treemap, OHLCV, news) refresh on a 12-hour TTL — the bucket is keyed off wall-clock time, so an entry stays warm until the next 12-hour boundary.

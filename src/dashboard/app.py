"""
Market Atlas — Streamlit dashboard.

Displays an interactive heatmap treemap of index constituents colored by
return %, and a per-stock candlestick chart with technical indicator overlays.

Usage:
    streamlit run src/dashboard/app.py

Reads from:
    src/config/configuration.json  (db_url)
    public.assets                  (symbol, gics_sector)
    public.daily_bars              (OHLCV time-series)
    public.sp500_constituents      (S&P 500 membership)
    public.nasdaq100_constituents  (NASDAQ-100 membership)
    public.dow30_constituents      (Dow 30 membership)

Dashboard panels:
    Heatmap        — treemap tiles sized by dollar volume, colored by return %
    Sector Synopsis — bar chart + KPIs for a chosen GICS sector
    Stock Detail   — candlestick chart with SMA 20/50, EMA 20, Bollinger Bands, RSI 14
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from collections import OrderedDict

import numpy as np
import pandas as pd
import plotly.colors as pc
import plotly.express as px
import plotly.graph_objects as go
import psycopg
import streamlit as st
import streamlit.components.v1 as st_components
from plotly.subplots import make_subplots

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.dashboard.indicators import (  # noqa: E402
    compute_bollinger_bands,
    compute_ema,
    compute_rsi,
    compute_sma,
)
from src.db.repositories import fetch_ohlcv  # noqa: E402
CONFIG_CANDIDATES = [
    REPO_ROOT / "src" / "config" / "config.json",
    REPO_ROOT / "src" / "config" / "configuration.json",
    REPO_ROOT / "config.json",
    REPO_ROOT / "configuration.json",
]


def load_config() -> dict:
    for p in CONFIG_CANDIDATES:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return {}


def connect(db_url: str) -> psycopg.Connection:
    return psycopg.connect(db_url)


INDEX_OPTIONS = {
    "S&P 500": "sp500",
    "NASDAQ-100": "nasdaq100",
    "Dow 30": "dow30",
    "All": "all",
}

# Preset definitions: label -> (days_back_from_today | None = YTD, display label)
_DATE_PRESETS: dict[str, tuple[int | None, str]] = {
    "3M":  (90,   "Past 3 months"),
    "6M":  (182,  "Past 6 months"),
    "1Y":  (365,  "Past 1 year"),
    "2Y":  (730,  "Past 2 years"),
    "YTD": (None, "YTD"),
}


def _preset_dates(key: str) -> tuple[dt.date, dt.date]:
    """Compute (date_from, date_to) for a preset key, anchored to today."""
    today = dt.date.today()
    days_back, _ = _DATE_PRESETS[key]
    d_from = (
        dt.date(today.year, 1, 1)
        if days_back is None
        else today - dt.timedelta(days=days_back)
    )
    return d_from, today


def _on_preset_click(label: str, min_day: dt.date, end_max_day: dt.date) -> None:
    """
    on_click callback for preset buttons.
    Runs before the script reruns, so active_preset is already set when
    the buttons are rendered — the highlight appears on the first click.
    """
    d_from, d_to = _preset_dates(label)
    st.session_state["active_preset"] = label
    st.session_state["date_from"] = max(min_day, min(d_from, end_max_day))
    st.session_state["date_to"] = max(min_day, min(d_to, end_max_day))


def build_universe_sql(index_key: str) -> str:
    # Normalize the universe into: symbol, group_name, index_name.
    # Sector always comes from assets.gics_sector (unified GICS standard).
    if index_key == "sp500":
        return """
        SELECT a.symbol,
               COALESCE(a.name, a.symbol) AS name,
               COALESCE(a.gics_sector, 'Unknown') AS group_name,
               'S&P 500' AS index_name
        FROM public.assets a
        JOIN public.sp500_constituents sc
          ON sc.symbol = a.symbol AND sc.is_active IS NOT FALSE
        """
    if index_key == "nasdaq100":
        return """
        SELECT a.symbol,
               COALESCE(a.name, a.symbol) AS name,
               COALESCE(a.gics_sector, 'Unknown') AS group_name,
               'NASDAQ-100' AS index_name
        FROM public.assets a
        JOIN public.nasdaq100_constituents nc
          ON nc.symbol = a.symbol AND nc.is_active IS NOT FALSE
        """
    if index_key == "dow30":
        return """
        SELECT a.symbol,
               COALESCE(a.name, a.symbol) AS name,
               COALESCE(a.gics_sector, 'Unknown') AS group_name,
               'Dow 30' AS index_name
        FROM public.assets a
        JOIN public.dow30_constituents dc
          ON dc.symbol = a.symbol AND dc.is_active IS NOT FALSE
        """
    # all indices — deduplicated at SQL level
    return """
    SELECT DISTINCT a.symbol,
           COALESCE(a.name, a.symbol) AS name,
           COALESCE(a.gics_sector, 'Unknown') AS group_name,
           'All' AS index_name
    FROM public.assets a
    WHERE a.symbol IN (
        SELECT symbol FROM public.sp500_constituents   WHERE is_active IS NOT FALSE
        UNION SELECT symbol FROM public.nasdaq100_constituents WHERE is_active IS NOT FALSE
        UNION SELECT symbol FROM public.dow30_constituents    WHERE is_active IS NOT FALSE
    )
    """


def fetch_treemap_data(
    conn: psycopg.Connection,
    index_key: str,
    date_from: dt.date,
    date_to: dt.date,
) -> pd.DataFrame:
    # We compute start/end close per symbol within [date_from, date_to]
    # using DISTINCT ON for fastest "first/last in range".
    universe_sql = build_universe_sql(index_key)

    q = f"""
    WITH universe AS (
        {universe_sql}
    ),
    start_px AS (
        SELECT DISTINCT ON (b.symbol)
            b.symbol,
            b.close AS start_close
        FROM public.daily_bars b
        JOIN universe u ON u.symbol = b.symbol
        WHERE ((b.ts AT TIME ZONE 'UTC')::date) BETWEEN %(date_from)s AND %(date_to)s
        ORDER BY b.symbol, b.ts ASC
    ),
    end_px AS (
        SELECT DISTINCT ON (b.symbol)
            b.symbol,
            b.close AS end_close,
            b.volume AS end_volume
        FROM public.daily_bars b
        JOIN universe u ON u.symbol = b.symbol
        WHERE ((b.ts AT TIME ZONE 'UTC')::date) BETWEEN %(date_from)s AND %(date_to)s
        ORDER BY b.symbol, b.ts DESC
    )
    SELECT
        u.index_name,
        u.group_name,
        u.symbol,
        u.name,
        s.start_close,
        e.end_close,
        e.end_volume,
        CASE
            WHEN s.start_close IS NULL OR e.end_close IS NULL OR s.start_close = 0 THEN NULL
            ELSE ((e.end_close - s.start_close) / s.start_close) * 100
        END AS return_pct,
        CASE
            WHEN e.end_close IS NULL OR e.end_volume IS NULL THEN NULL
            ELSE (e.end_close * e.end_volume)
        END AS dollar_volume
    FROM universe u
    LEFT JOIN start_px s ON s.symbol = u.symbol
    LEFT JOIN end_px e ON e.symbol = u.symbol
    """

    df = pd.read_sql(q, conn, params={"date_from": date_from, "date_to": date_to})

    # If union universe, dedupe symbols for visualization (avoid repeated tiles).
    # Prefer the first occurrence by index_name order. (You can change preference later.)
    if index_key == "all":
        df = df.sort_values(["symbol", "index_name"]).drop_duplicates(
            "symbol", keep="first"
        )

    # Keep only rows where we have prices
    df = df.dropna(subset=["return_pct", "dollar_volume"])
    return df


# Helper to fetch available date bounds for the selected universe
@st.cache_data(show_spinner=False, ttl=60)
def fetch_available_date_bounds(
    db_url: str, index_key: str
) -> tuple[dt.date | None, dt.date | None]:
    """
    Returns (min_date, max_date) available in daily_bars for symbols in the selected universe.
    Cached briefly to keep the UI responsive.
    """
    universe_sql = build_universe_sql(index_key)

    q = f"""
    WITH universe AS (
        {universe_sql}
    )
    SELECT
        min((b.ts AT TIME ZONE 'UTC'))::date AS min_day,
        max((b.ts AT TIME ZONE 'UTC'))::date AS max_day
    FROM public.daily_bars b
    JOIN universe u ON u.symbol = b.symbol
    """

    with connect(db_url) as conn:
        df = pd.read_sql(q, conn)
    if df.empty:
        return None, None
    return df.loc[0, "min_day"], df.loc[0, "max_day"]


def build_fig(df: pd.DataFrame, color_range: tuple[float, float]) -> px.treemap:
    fig = px.treemap(
        df,
        path=["group_name", "symbol"],
        values="dollar_volume",
        color="return_pct",
        hover_data={
            "start_close": ":.2f",
            "end_close": ":.2f",
            "dollar_volume": ":,.0f",
            "index_name": True,
        },
        color_continuous_scale="RdYlGn",
        range_color=color_range,
    )

    fig.update_traces(
        marker=dict(line=dict(width=0)),  # no borders
        hovertemplate=(
            "<b>%{label}</b><br>"
            "Return: %{color:.2f}%<br>"
            "Start close: %{customdata[0]:.2f}<br>"
            "End close: %{customdata[1]:.2f}<br>"
            "Dollar volume: %{customdata[2]:,.0f}<br>"
            "Index: %{customdata[3]}<br>"
            "<extra></extra>"
        ),
    )

    fig.update_layout(
        height=640,  # slightly shorter to fit viewport
        margin=dict(t=20, l=6, r=6, b=6),
        coloraxis_colorbar=dict(
            title="Return %",
            len=0.85,  # taller colorbar
        ),
        hoverlabel=dict(
            bgcolor="white",
            font_size=13,
            font_color="black",
        ),
    )

    return fig


# --- In-memory LRU cache for treemap data (per session) ---
def _get_session_cache() -> "OrderedDict[tuple, pd.DataFrame]":
    # LRU cache stored in Streamlit session_state (per browser session).
    if "treemap_cache" not in st.session_state:
        st.session_state["treemap_cache"] = OrderedDict()
    return st.session_state["treemap_cache"]


def get_treemap_data_cached(
    db_url: str,
    index_key: str,
    date_from: dt.date,
    date_to: dt.date,
    cache_size: int = 12,
) -> pd.DataFrame:
    """
    In-memory LRU cache for treemap data. Prevents repeated DB queries when toggling filters back/forth.
    Cached per Streamlit session.

    If cache_size == 0, caching is disabled (always hits the DB).
    """
    key = (db_url, index_key, date_from.isoformat(), date_to.isoformat())

    # Track cache stats for display (per session)
    if "treemap_cache_hits" not in st.session_state:
        st.session_state["treemap_cache_hits"] = 0
    if "treemap_cache_misses" not in st.session_state:
        st.session_state["treemap_cache_misses"] = 0

    if cache_size <= 0:
        st.session_state["treemap_cache_misses"] += 1
        with connect(db_url) as conn:
            return fetch_treemap_data(
                conn, index_key=index_key, date_from=date_from, date_to=date_to
            )

    cache = _get_session_cache()

    if key in cache:
        st.session_state["treemap_cache_hits"] += 1
        cache.move_to_end(key)  # mark as recently used
        return cache[key]

    st.session_state["treemap_cache_misses"] += 1

    # Cache miss -> query DB
    with connect(db_url) as conn:
        df = fetch_treemap_data(
            conn, index_key=index_key, date_from=date_from, date_to=date_to
        )

    cache[key] = df
    cache.move_to_end(key)

    # Enforce LRU size
    while len(cache) > cache_size:
        cache.popitem(last=False)

    return df


# --- In-memory LRU cache for OHLCV data (per session) ---
def _get_ohlcv_cache() -> "OrderedDict[tuple, pd.DataFrame]":
    if "ohlcv_cache" not in st.session_state:
        st.session_state["ohlcv_cache"] = OrderedDict()
    return st.session_state["ohlcv_cache"]


def get_ohlcv_cached(
    db_url: str,
    symbol: str,
    date_from: dt.date,
    date_to: dt.date,
    cache_size: int = 20,
) -> pd.DataFrame:
    key = (symbol, date_from.isoformat(), date_to.isoformat())
    cache = _get_ohlcv_cache()
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    with connect(db_url) as conn:
        df = fetch_ohlcv(conn, symbol=symbol, date_from=date_from, date_to=date_to)
    cache[key] = df
    cache.move_to_end(key)
    while len(cache) > cache_size:
        cache.popitem(last=False)
    return df


def build_detail_fig(
    df: pd.DataFrame, symbol: str, active: list[str]
) -> go.Figure:
    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.60, 0.20, 0.20],
        subplot_titles=("", "Volume", "RSI (14)"),
    )

    close = df["close"]
    dates = df["date"]

    # Row 1 — Candlestick
    fig.add_trace(
        go.Candlestick(
            x=dates,
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=close,
            name=symbol,
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
        ),
        row=1, col=1,
    )

    # Overlay indicators on row 1
    if "SMA 20" in active:
        fig.add_trace(
            go.Scatter(x=dates, y=compute_sma(close, 20), name="SMA 20",
                       line=dict(color="#1976D2", width=1.2)),
            row=1, col=1,
        )
    if "SMA 50" in active:
        fig.add_trace(
            go.Scatter(x=dates, y=compute_sma(close, 50), name="SMA 50",
                       line=dict(color="#F57C00", width=1.2)),
            row=1, col=1,
        )
    if "EMA 20" in active:
        fig.add_trace(
            go.Scatter(x=dates, y=compute_ema(close, 20), name="EMA 20",
                       line=dict(color="#7B1FA2", width=1.2, dash="dot")),
            row=1, col=1,
        )
    if "Bollinger Bands" in active:
        bb = compute_bollinger_bands(close)
        fig.add_trace(
            go.Scatter(x=dates, y=bb["bb_upper"], name="BB Upper",
                       line=dict(color="#78909C", width=1, dash="dash"), showlegend=True),
            row=1, col=1,
        )
        fig.add_trace(
            go.Scatter(x=dates, y=bb["bb_mid"], name="BB Mid",
                       line=dict(color="#78909C", width=0.8, dash="dot"), showlegend=False),
            row=1, col=1,
        )
        fig.add_trace(
            go.Scatter(x=dates, y=bb["bb_lower"], name="BB Lower",
                       line=dict(color="#78909C", width=1, dash="dash"),
                       fill="tonexty", fillcolor="rgba(120,144,156,0.08)", showlegend=False),
            row=1, col=1,
        )

    # Row 2 — Volume bars coloured by up/down
    bar_colors = np.where(df["close"].values >= df["open"].values, "#26a69a", "#ef5350")
    fig.add_trace(
        go.Bar(x=dates, y=df["volume"], name="Volume",
               marker_color=bar_colors, showlegend=False),
        row=2, col=1,
    )

    # Row 3 — RSI
    if "RSI" in active:
        rsi = compute_rsi(close)
        fig.add_trace(
            go.Scatter(x=dates, y=rsi, name="RSI 14",
                       line=dict(color="#FF6F00", width=1.5), showlegend=False),
            row=3, col=1,
        )
        fig.add_hline(y=70, line_dash="dot", line_color="red", row=3, col=1)
        fig.add_hline(y=30, line_dash="dot", line_color="green", row=3, col=1)

    fig.update_layout(
        height=800,
        margin=dict(t=30, l=60, r=20, b=20),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    fig.update_yaxes(title_text="RSI", row=3, col=1, range=[0, 100])

    return fig


@st.fragment
def render_sector_synopsis(
    df: pd.DataFrame,
    sector: str,
    range_label: str,
    db_url: str,
    date_from: dt.date,
    date_to: dt.date,
) -> None:
    """Render KPIs + ranked bar chart for a single GICS sector."""
    sdf = df[df["group_name"] == sector].copy()

    if sdf.empty:
        st.warning(f"No data for sector: {sector}")
        return

    sdf = sdf.sort_values("return_pct", ascending=False).reset_index(drop=True)

    median_ret = sdf["return_pct"].median()
    avg_ret = sdf["return_pct"].mean()
    gainers = (sdf["return_pct"] > 0).sum()
    losers = (sdf["return_pct"] < 0).sum()
    total_dvol = sdf["dollar_volume"].sum()
    best = sdf.iloc[0]
    worst = sdf.iloc[-1]

    # KPI row
    k = st.columns(5)
    k[0].metric("Stocks", len(sdf))
    k[1].metric(f"Median return ({range_label})", f"{median_ret:+.2f}%")
    k[2].metric(f"Avg return ({range_label})", f"{avg_ret:+.2f}%")
    k[3].metric("Gainers / Losers", f"{gainers} / {losers}")
    k[4].metric("Total dollar volume", f"${total_dvol:,.0f}")

    st.markdown("---")

    # Plain-text summary
    direction = "outperformed" if avg_ret > 0 else "underperformed"
    st.markdown(
        f"**{sector}** had **{len(sdf)} stocks** in the selected period. "
        f"The sector {direction} with an average return of **{avg_ret:+.2f}%** "
        f"(median: **{median_ret:+.2f}%**). "
        f"Best performer: **{best['symbol']}** ({best['return_pct']:+.2f}%). "
        f"Worst performer: **{worst['symbol']}** ({worst['return_pct']:+.2f}%)."
    )

    st.markdown("---")

    # Horizontal bar chart — all stocks ranked by return %
    bar_colors = ["#26a69a" if r >= 0 else "#ef5350" for r in sdf["return_pct"]]

    fig = go.Figure(go.Bar(
        x=sdf["return_pct"],
        y=sdf["symbol"],
        orientation="h",
        marker_color=bar_colors,
        text=[f"{r:+.2f}%" for r in sdf["return_pct"]],
        textposition="outside",
        hovertemplate=(
            "<b>%{y}</b><br>"
            "Return: %{x:.2f}%<br>"
            "<extra></extra>"
        ),
    ))

    fig.add_vline(x=0, line_color="white", line_width=1)
    fig.add_vline(
        x=avg_ret,
        line_color="yellow",
        line_width=1.5,
        line_dash="dash",
        annotation_text=f"avg {avg_ret:+.2f}%",
        annotation_position="top right",
        annotation_font_color="yellow",
    )

    fig.update_layout(
        height=max(420, len(sdf) * 26),
        margin=dict(t=30, l=80, r=80, b=20),
        xaxis_title="Return %",
        yaxis=dict(autorange="reversed", tickfont=dict(size=11)),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font_color="white",
        xaxis=dict(gridcolor="rgba(255,255,255,0.1)", zeroline=False),
    )

    event = st.plotly_chart(
        fig,
        use_container_width=True,
        theme=None,
        on_select="rerun",
        key=f"sector_bar_{sector}",
    )

    # Inline stock detail when a bar is clicked
    clicked_points = (event.selection or {}).get("points", [])
    if clicked_points:
        clicked_symbol = clicked_points[0].get("y")
        if clicked_symbol:
            # Named anchor so JS can scroll to it reliably
            st.markdown(
                '<div id="sector-stock-detail"></div>', unsafe_allow_html=True
            )
            st_components.html(
                """
                <script>
                  const el = window.parent.document.getElementById('sector-stock-detail');
                  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
                </script>
                """,
                height=0,
            )

            row = sdf[sdf["symbol"] == clicked_symbol].iloc[0]
            st.markdown(f"### {clicked_symbol} — {row.get('name', clicked_symbol)}")
            st.markdown(
                f"Return: **{row['return_pct']:+.2f}%** &nbsp;|&nbsp; "
                f"Start: **${row['start_close']:.2f}** &nbsp;|&nbsp; "
                f"End: **${row['end_close']:.2f}** &nbsp;|&nbsp; "
                f"Dollar volume: **${row['dollar_volume']:,.0f}**"
            )
            with st.spinner(f"Loading {clicked_symbol}..."):
                df_ohlcv = get_ohlcv_cached(db_url, clicked_symbol, date_from, date_to)
            if df_ohlcv.empty:
                st.warning("No price data in the selected range.")
            else:
                st.plotly_chart(
                    build_detail_fig(df_ohlcv, clicked_symbol, ["SMA 20", "SMA 50"]),
                    use_container_width=True,
                    theme=None,
                )

    with st.expander("Show raw data"):
        display = sdf[["symbol", "name", "return_pct", "start_close", "end_close", "dollar_volume"]].copy()
        display.columns = ["Symbol", "Name", "Return %", "Start Close", "End Close", "Dollar Volume"]
        display["Return %"] = display["Return %"].map(lambda x: f"{x:+.2f}%")
        display["Start Close"] = display["Start Close"].map(lambda x: f"${x:.2f}")
        display["End Close"] = display["End Close"].map(lambda x: f"${x:.2f}")
        display["Dollar Volume"] = display["Dollar Volume"].map(lambda x: f"${x:,.0f}")
        st.dataframe(display, use_container_width=True, hide_index=True)


def render_ranked_table(df: pd.DataFrame, color_range: tuple[float, float]) -> None:
    """
    Ranked table view of all symbols in the current universe.

    Reuses fetch_treemap_data output — no extra queries.
    Columns: rank, symbol, name, sector, return %, percentile rank,
             dollar volume, volume, start close, end close.
    Percentile rank = rank / count * 100 (100 = best performer).
    Return % column is background-colored with the same RdYlGn palette
    as the treemap, clipped to the same ±X range.
    """
    tdf = df[[
        "symbol", "name", "group_name",
        "return_pct", "dollar_volume", "end_volume",
        "start_close", "end_close",
    ]].copy()

    # Percentile rank across the whole universe (100 = top performer)
    tdf["percentile_rank"] = (tdf["return_pct"].rank(ascending=True) / len(tdf) * 100)

    # Default sort: best return first; user can re-sort by clicking headers
    tdf = tdf.sort_values("return_pct", ascending=False).reset_index(drop=True)
    tdf.index = tdf.index + 1  # 1-based rank shown as the index column

    tdf = tdf.rename(columns={
        "symbol":         "Symbol",
        "name":           "Name",
        "group_name":     "Sector",
        "return_pct":     "Return %",
        "percentile_rank":"Percentile",
        "dollar_volume":  "Dollar Volume",
        "end_volume":     "Volume",
        "start_close":    "Start Close",
        "end_close":      "End Close",
    })

    vmin, vmax = color_range

    def _return_cell_style(val: float) -> str:
        """Map a return % value to a RdYlGn cell background using Plotly (no matplotlib)."""
        span = vmax - vmin
        t = max(0.0, min(1.0, (val - vmin) / span)) if span else 0.5
        rgb = pc.sample_colorscale("RdYlGn", [t])[0]  # "rgb(r, g, b)"
        return f"background-color: {rgb}; color: black"

    styled = (
        tdf.style
        # Same RdYlGn palette + clip range as the treemap — no matplotlib needed
        .map(_return_cell_style, subset=["Return %"])
        # Horizontal bar for percentile: red at 0, green at 100
        .bar(
            subset=["Percentile"],
            color=["#ef5350", "#26a69a"],
            vmin=0,
            vmax=100,
        )
        .format({
            "Return %":     "{:+.2f}%",
            "Percentile":   "{:.0f}",
            "Dollar Volume":"${:,.0f}",
            "Volume":       "{:,.0f}",
            "Start Close":  "${:.2f}",
            "End Close":    "${:.2f}",
        })
    )

    st.caption(
        f"{len(tdf)} symbols · sortable by any column · "
        "color scale clipped to the same ±range as the treemap"
    )
    st.dataframe(styled, use_container_width=True)


def main() -> None:
    st.set_page_config(page_title="Market Heatmap", layout="wide")
    st.markdown(
        "<style>"
        ".block-container { padding-top: 1rem; padding-bottom: 0.5rem; }"
        ".stMetric { padding: 0.25rem 0.5rem; }"
        ".stSidebar .block-container { padding-top: 0.75rem; }"
        "</style>",
        unsafe_allow_html=True,
    )
    st.title("Market Heatmap Dashboard")

    cfg = load_config()

    # Sidebar controls
    st.sidebar.header("Filters")

    # DB URL comes from configuration only (not exposed in the UI).
    db_url = (cfg.get("db_url") or "").strip()

    if not db_url:
        st.error(
            "Missing database configuration. Please set 'db_url' in "
            "src/config/configuration.json (or src/config/config.json)."
        )
        st.stop()

    index_label = st.sidebar.selectbox(
        "Index universe", list(INDEX_OPTIONS.keys()), index=3
    )
    index_key = INDEX_OPTIONS[index_label]

    # Limit the date picker to available data for the selected universe
    min_day, max_day = fetch_available_date_bounds(db_url=db_url, index_key=index_key)
    if not min_day or not max_day:
        st.warning("No price data found for this universe yet.")
        st.stop()

    # End date should be capped at max_day (e.g., 2026-01-30) per UI requirement.
    end_max_day = max(min_day, max_day)

    # ---------- Quick-range preset buttons ----------
    # Default to "3M" on first load
    if "active_preset" not in st.session_state:
        st.session_state["active_preset"] = "3M"

    # Render buttons with on_click callback so active_preset is updated
    # before the rerun — button highlights on the very first click.
    st.sidebar.subheader("Quick range")
    btn_cols = st.sidebar.columns(len(_DATE_PRESETS))
    for col, label in zip(btn_cols, _DATE_PRESETS):
        col.button(
            label,
            key=f"preset_btn_{label}",
            type="primary" if st.session_state["active_preset"] == label else "secondary",
            use_container_width=True,
            on_click=_on_preset_click,
            kwargs={"label": label, "min_day": min_day, "end_max_day": end_max_day},
        )

    # Seed session_state on very first load (no button clicked yet)
    if not isinstance(st.session_state.get("date_from"), dt.date):
        d_from, d_to = _preset_dates(st.session_state["active_preset"])
        st.session_state["date_from"] = max(min_day, min(d_from, end_max_day))
        st.session_state["date_to"] = max(min_day, min(d_to, end_max_day))

    # Clamp stored dates to available bounds before rendering pickers
    st.session_state["date_from"] = max(min_day, min(st.session_state["date_from"], end_max_day))
    st.session_state["date_to"] = max(min_day, min(st.session_state["date_to"], end_max_day))

    st.sidebar.subheader("Date range")
    date_from = st.sidebar.date_input(
        "Start date",
        min_value=min_day,
        max_value=max_day,
        key="date_from",
        help=f"Available data: {min_day} to {max_day}",
    )

    date_to = st.sidebar.date_input(
        "End date",
        min_value=min_day,
        max_value=end_max_day,
        key="date_to",
        help=f"Available data: {min_day} to {end_max_day}",
    )

    # Clear highlight when the user manually edits either date picker
    _active = st.session_state.get("active_preset")
    if _active:
        _exp_from, _exp_to = _preset_dates(_active)
        _exp_from = max(min_day, min(_exp_from, end_max_day))
        _exp_to = max(min_day, min(_exp_to, end_max_day))
        if (date_from, date_to) != (_exp_from, _exp_to):
            st.session_state["active_preset"] = None

    if date_from > date_to:
        st.error("Start date must be <= end date.")
        st.stop()

    # Clamp to available data bounds for querying
    clamped_from = max(min_day, min(date_from, max_day))
    clamped_to = max(min_day, min(date_to, end_max_day))

    if (clamped_from, clamped_to) != (date_from, date_to):
        st.info(f"Clamped date range to available data: {min_day} to {max_day}.")
        date_from, date_to = clamped_from, clamped_to
        st.session_state["date_from"] = date_from
        st.session_state["date_to"] = date_to

    # Range label used in KPI headings
    _active = st.session_state.get("active_preset")
    if _active and _active in _DATE_PRESETS:
        range_label = _DATE_PRESETS[_active][1]
    else:
        days_span = (date_to - date_from).days + 1
        range_label = f"{days_span}d ({date_from.isoformat()} → {date_to.isoformat()})"

    # Color range control (so the heatmap isn't blown out by outliers)
    st.sidebar.subheader("Color scaling")
    clip = st.sidebar.slider("Clip return % to ±X", min_value=1, max_value=50, value=10)
    color_range = (-float(clip), float(clip))

    # Cache: fixed size (not user-configurable)
    cache_size = 24
    st.sidebar.subheader("Cache")
    st.sidebar.caption(
        f"Entries: {len(_get_session_cache())} | "
        f"Hits: {st.session_state.get('treemap_cache_hits', 0)} | "
        f"Misses: {st.session_state.get('treemap_cache_misses', 0)}"
    )

    if st.sidebar.button("Clear cached results"):
        _get_session_cache().clear()
        _get_ohlcv_cache().clear()
        st.session_state["treemap_cache_hits"] = 0
        st.session_state["treemap_cache_misses"] = 0
        st.sidebar.success("Cleared cache")

    with st.spinner("Loading data..."):
        df = get_treemap_data_cached(
            db_url=db_url,
            index_key=index_key,
            date_from=date_from,
            date_to=date_to,
            cache_size=cache_size,
        )

    if df.empty:
        st.warning(
            "No data returned for this range/universe. Try expanding the date range."
        )
        st.stop()

    tab_heatmap, tab_synopsis, tab_detail = st.tabs(
        ["Heatmap", "Sector Synopsis", "Stock Detail"]
    )

    with tab_heatmap:
        # Top KPIs
        cols = st.columns(4)
        cols[0].metric("Symbols", f"{len(df)}")
        cols[1].metric(
            f"Median return ({range_label})", f"{df['return_pct'].median():.2f}%"
        )
        cols[2].metric(
            f"Best ({range_label})",
            f"{df.loc[df['return_pct'].idxmax(), 'symbol']} ({df['return_pct'].max():.2f}%)",
        )
        cols[3].metric(
            f"Worst ({range_label})",
            f"{df.loc[df['return_pct'].idxmin(), 'symbol']} ({df['return_pct'].min():.2f}%)",
        )

        view_toggle = st.radio(
            "View",
            ["Treemap", "Ranked Table"],
            horizontal=True,
            key="heatmap_view_toggle",
            label_visibility="collapsed",
        )

        if view_toggle == "Treemap":
            fig = build_fig(df, color_range=color_range)
            st.plotly_chart(fig, use_container_width=True, theme=None)

            with st.expander("Show raw data"):
                st.dataframe(
                    df.sort_values("return_pct", ascending=False).reset_index(drop=True),
                    use_container_width=True,
                )
        else:
            render_ranked_table(df, color_range)

    with tab_synopsis:
        sectors = sorted(df["group_name"].dropna().unique())
        if not sectors:
            st.warning("No sector data available for this universe.")
        else:
            selected_sector = st.selectbox(
                "Select sector",
                sectors,
                key="synopsis_sector",
            )
            render_sector_synopsis(df, selected_sector, range_label, db_url, date_from, date_to)

    with tab_detail:  # Stock Detail
        # Build symbol options with return % for context
        symbol_returns = (
            df[["symbol", "group_name", "return_pct"]]
            .sort_values("symbol")
            .reset_index(drop=True)
        )
        symbol_options = [
            f"{row.symbol}  ({row.group_name}, {row.return_pct:+.1f}%)"
            for row in symbol_returns.itertuples()
        ]
        symbol_map = dict(zip(symbol_options, symbol_returns["symbol"]))

        search = st.text_input(
            "Search symbol",
            placeholder="Type to filter (e.g. AAPL, MSFT, Tech...)",
            key="symbol_search",
        )

        if search:
            query = search.upper()
            filtered = [s for s in symbol_options if query in s.upper()]
        else:
            filtered = symbol_options

        if not filtered:
            st.warning(f"No symbols match '{search}'.")
            st.stop()

        # Persist selection across reruns even when the filtered list changes
        prev = st.session_state.get("detail_symbol_display")
        if prev in filtered:
            default_idx = filtered.index(prev)
        else:
            default_idx = 0

        selected_display = st.selectbox(
            "Select symbol",
            filtered,
            index=default_idx,
            key="detail_symbol_display",
        )
        selected_symbol = symbol_map[selected_display]

        active_indicators = st.multiselect(
            "Overlays",
            ["SMA 20", "SMA 50", "EMA 20", "Bollinger Bands", "RSI"],
            default=["SMA 20", "SMA 50"],
            key="detail_indicators",
        )

        with st.spinner(f"Loading {selected_symbol}..."):
            df_ohlcv = get_ohlcv_cached(
                db_url, selected_symbol, date_from, date_to
            )

        if df_ohlcv.empty:
            st.warning("No price data found for this symbol in the selected date range.")
        else:
            if len(df_ohlcv) < 21:
                st.warning(
                    f"Only {len(df_ohlcv)} bars in range — some indicators need more data "
                    "(Bollinger Bands: 21, SMA 50: 50). Extend the date range for complete signals."
                )
            st.plotly_chart(
                build_detail_fig(df_ohlcv, selected_symbol, active_indicators),
                use_container_width=True,
                theme=None,
            )


if __name__ == "__main__":
    main()

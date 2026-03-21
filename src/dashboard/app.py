from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from collections import OrderedDict

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import psycopg
import streamlit as st
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


def build_universe_sql(index_key: str) -> str:
    # Normalize the universe into: symbol, group_name, index_name
    # group_name is used as the treemap "sector/industry" bucket.
    if index_key == "sp500":
        return """
        SELECT
            symbol,
            COALESCE(NULLIF(gics_sector, ''), 'Unknown') AS group_name,
            'S&P 500' AS index_name
        FROM public.sp500_constituents
        """
    if index_key == "nasdaq100":
        return """
        SELECT
            symbol,
            COALESCE(icb_industry, 'Unknown') AS group_name,
            'NASDAQ-100' AS index_name
        FROM public.nasdaq100_constituents
        """
    if index_key == "dow30":
        return """
        SELECT
            symbol,
            COALESCE(industry, 'Unknown') AS group_name,
            'Dow 30' AS index_name
        FROM public.dow30_constituents
        """
    # union
    return """
    SELECT symbol, group_name, index_name
    FROM (
        SELECT
            symbol,
            COALESCE(NULLIF(gics_sector, ''), 'Unknown') AS group_name,
            'S&P 500' AS index_name
        FROM public.sp500_constituents
        UNION ALL
        SELECT
            symbol,
            COALESCE(icb_industry, 'Unknown') AS group_name,
            'NASDAQ-100' AS index_name
        FROM public.nasdaq100_constituents
        UNION ALL
        SELECT
            symbol,
            COALESCE(industry, 'Unknown') AS group_name,
            'Dow 30' AS index_name
        FROM public.dow30_constituents
    ) u
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

    st.sidebar.subheader("Quick range")
    preset = st.sidebar.selectbox(
        "Select range",
        [
            "Custom",
            "Past 3 months",
            "Past 6 months",
            "Past 1 year",
            "Past 2 years",
            "YTD",
        ],
        index=1,
        key="range_preset",
        help="Pick a preset to quickly set start/end dates. Choose Custom to set dates manually.",
    )

    # Apply preset BEFORE rendering the date inputs (so Streamlit doesn't complain about out-of-range values).
    if preset != "Custom":
        date_to_preset = end_max_day

        if preset == "Past 3 months":
            date_from_preset = date_to_preset - dt.timedelta(days=90)
        elif preset == "Past 6 months":
            date_from_preset = date_to_preset - dt.timedelta(days=182)
        elif preset == "Past 1 year":
            date_from_preset = date_to_preset - dt.timedelta(days=365)
        elif preset == "Past 2 years":
            date_from_preset = date_to_preset - dt.timedelta(days=730)
        elif preset == "YTD":
            date_from_preset = dt.date(date_to_preset.year, 1, 1)
        else:
            date_from_preset = date_to_preset - dt.timedelta(days=90)

        # Clamp to available data bounds.
        st.session_state["date_to"] = max(min_day, min(date_to_preset, end_max_day))
        st.session_state["date_from"] = max(min_day, min(date_from_preset, max_day))

    # Defaults: last 3 months ending at fixed 01/30/2026 (clamped to available bounds).
    fixed_default_to = dt.date(2026, 1, 30)
    default_to = min(end_max_day, max(min_day, fixed_default_to))
    default_from = max(min_day, default_to - dt.timedelta(days=90))

    # Seed session_state with valid defaults if missing/invalid.
    if not isinstance(st.session_state.get("date_from"), dt.date):
        st.session_state["date_from"] = default_from
    if not isinstance(st.session_state.get("date_to"), dt.date):
        st.session_state["date_to"] = default_to

    # Clamp any previously selected dates BEFORE rendering widgets.
    st.session_state["date_from"] = max(
        min_day, min(st.session_state["date_from"], max_day)
    )
    st.session_state["date_to"] = max(
        min_day, min(st.session_state["date_to"], end_max_day)
    )

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

    # If user manually edits dates, reflect that by switching preset to Custom.
    if st.session_state.get("range_preset") != "Custom":
        # Compare against what the preset would imply (using end_max_day).
        # If the user changes either bound, treat as Custom.
        expected_to = end_max_day
        preset = st.session_state.get("range_preset")

        if preset == "Past 3 months":
            expected_from = expected_to - dt.timedelta(days=90)
        elif preset == "Past 6 months":
            expected_from = expected_to - dt.timedelta(days=182)
        elif preset == "Past 1 year":
            expected_from = expected_to - dt.timedelta(days=365)
        elif preset == "Past 2 years":
            expected_from = expected_to - dt.timedelta(days=730)
        elif preset == "YTD":
            expected_from = dt.date(expected_to.year, 1, 1)
        else:
            expected_from = expected_to - dt.timedelta(days=90)

        expected_from = max(min_day, min(expected_from, max_day))
        expected_to = max(min_day, min(expected_to, end_max_day))

        if (date_from, date_to) != (expected_from, expected_to):
            st.session_state["range_preset"] = "Custom"

    if date_from > date_to:
        st.error("Start date must be <= end date.")
        st.stop()

    # Clamp to available data bounds for querying.
    clamped_from = max(min_day, min(date_from, max_day))
    clamped_to = max(min_day, min(date_to, end_max_day))

    if (clamped_from, clamped_to) != (date_from, date_to):
        st.info(f"Clamped date range to available data: {min_day} to {max_day}.")
        date_from, date_to = clamped_from, clamped_to
        st.session_state["date_from"] = date_from
        st.session_state["date_to"] = date_to

    # Build a label for KPIs based on the selected range/preset
    preset_label = st.session_state.get("range_preset", "Custom")
    if preset_label and preset_label != "Custom":
        range_label = preset_label
    else:
        days = (date_to - date_from).days + 1
        range_label = f"{days}d ({date_from.isoformat()} → {date_to.isoformat()})"

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

    active_view = st.radio(
        "View",
        ["Heatmap", "Stock Detail"],
        horizontal=True,
        key="active_view",
        label_visibility="collapsed",
    )

    if active_view == "Heatmap":
        # Top KPIs
        st.markdown("<div style='margin-top:-10px'></div>", unsafe_allow_html=True)
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

        fig = build_fig(df, color_range=color_range)
        st.plotly_chart(fig, use_container_width=True, theme=None)

        with st.expander("Show raw data"):
            st.dataframe(
                df.sort_values("return_pct", ascending=False).reset_index(drop=True),
                use_container_width=True,
            )

    else:  # Stock Detail
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

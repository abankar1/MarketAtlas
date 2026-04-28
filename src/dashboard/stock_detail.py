"""
Stock Detail tab — per-symbol candlestick chart with technical indicator overlays.

render_stock_detail(df, db_url, date_from, date_to)
    @st.fragment — symbol selectbox + 3-panel chart.
    Fragment scope means symbol changes stay within this panel and do not
    reset the active tab.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from src.dashboard.charts import build_compare_fig, build_detail_fig
from src.dashboard.data import fetch_index_overlap, get_ohlcv_cached


_INDEX_BADGE_STYLE = (
    "display:inline-block;padding:2px 10px;border-radius:12px;"
    "font-size:0.78em;font-weight:600;margin-right:6px;color:white;"
)
_INDEX_BADGES = [
    ("in_sp500",    "S&P 500",    "#1565C0"),
    ("in_nasdaq100","NASDAQ-100", "#6A1B9A"),
    ("in_dow30",    "Dow 30",     "#00695C"),
]



@st.fragment
def render_stock_detail(
    df: pd.DataFrame,
    db_url: str,
    date_from: dt.date,
    date_to: dt.date,
) -> None:
    """
    Render the Stock Detail panel for one symbol.

    Symbol selection persists across date-range changes by storing only the raw
    ticker in session_state["detail_selected_ticker"] — not the full label string
    (which changes when return % values are rebuilt after a date change).

    The selectbox uses index= (no key=) to avoid Streamlit's validation that
    session_state[key] must literally appear in the options list; that check fires
    before any Python code can correct a stale value and would cause an exception
    that resets the active tab.
    """
    symbol_returns = (
        df[["symbol", "name", "group_name", "return_pct"]]
        .sort_values("symbol")
        .reset_index(drop=True)
    )

    # Chart mode toggle — shown first so Compare mode can skip the primary selectbox
    chart_mode = st.radio(
        "Chart mode",
        ["Candlestick", "Compare"],
        horizontal=True,
        key="detail_chart_mode",
        label_visibility="collapsed",
    )

    # ----------------------------------------------------------------
    # Compare mode — single multiselect, no primary distinction
    # ----------------------------------------------------------------
    if chart_mode == "Compare":
        _all_options = [
            f"{row.symbol} — {row.name}"
            for row in symbol_returns.itertuples()
        ]
        _all_map = {
            f"{row.symbol} — {row.name}": row.symbol
            for row in symbol_returns.itertuples()
        }

        compare_displays = st.multiselect(
            "Symbols to compare",
            options=_all_options,
            max_selections=5,
            key="detail_compare_symbols",
            placeholder="Add up to 5 symbols…",
        )
        compare_symbols = [_all_map[d] for d in compare_displays]

        if not compare_symbols:
            st.info("Select at least one symbol above to start comparing.")
        else:
            with st.spinner("Loading data…"):
                series: dict[str, pd.DataFrame] = {
                    sym: get_ohlcv_cached(db_url, sym, date_from, date_to)
                    for sym in compare_symbols
                }
            series = {s: d for s, d in series.items() if not d.empty}

            if not series:
                st.warning("No price data found for the selected symbols in this date range.")
            else:
                st.caption("Each series rebased to 100 at the first bar of the date range.")
                st.plotly_chart(
                    build_compare_fig(series, primary=compare_symbols[0]),
                    use_container_width=True,
                    theme=None,
                )

    # ----------------------------------------------------------------
    # Candlestick mode
    # ----------------------------------------------------------------
    else:
        # Build option labels: "AAPL — Apple Inc.  (Information Technology, +12.3%)"
        symbol_options = [
            f"{row.symbol} — {row.name}  ({row.group_name}, {row.return_pct:+.1f}%)"
            for row in symbol_returns.itertuples()
        ]
        symbol_map = dict(zip(symbol_options, symbol_returns["symbol"]))
        ticker_to_option: dict[str, str] = {sym: opt for opt, sym in symbol_map.items()}

        _stored_ticker = st.session_state.get("detail_selected_ticker", "AAPL")
        if _stored_ticker and _stored_ticker in ticker_to_option:
            _default_idx = symbol_options.index(ticker_to_option[_stored_ticker])
        else:
            _default_idx = 0

        selected_display = st.selectbox(
            "Select symbol",
            symbol_options,
            index=_default_idx,
            help="Click and type to search by ticker, company name, or sector.",
        )
        selected_symbol = symbol_map[selected_display]
        st.session_state["detail_selected_ticker"] = selected_symbol

        # Index membership badges
        _overlap = fetch_index_overlap(db_url)
        _sym_row = _overlap[_overlap["symbol"] == selected_symbol]
        if not _sym_row.empty:
            _row = _sym_row.iloc[0]
            _html = "".join(
                f'<span style="{_INDEX_BADGE_STYLE}background:{color}">{label}</span>'
                for col, label, color in _INDEX_BADGES
                if _row[col]
            )
            if _html:
                st.markdown(_html, unsafe_allow_html=True)

        active_indicators = st.multiselect(
            "Overlays",
            ["SMA 20", "SMA 50", "EMA 20", "Bollinger Bands", "RSI", "MACD", "ATR", "OBV"],
            default=["SMA 20", "SMA 50"],
            key="detail_indicators",
        )

        with st.spinner(f"Loading {selected_symbol}…"):
            df_ohlcv = get_ohlcv_cached(db_url, selected_symbol, date_from, date_to)

        if df_ohlcv.empty:
            st.warning("No price data found for this symbol in the selected date range.")
        else:
            _n = len(df_ohlcv)
            _needs = []
            if _n < 50 and "SMA 50" in active_indicators:
                _needs.append("SMA 50: 50 bars")
            if _n < 21 and "Bollinger Bands" in active_indicators:
                _needs.append("Bollinger Bands: 21 bars")
            if _n < 15 and "RSI" in active_indicators:
                _needs.append("RSI: 15 bars")
            if _n < 35 and "MACD" in active_indicators:
                _needs.append("MACD: 35 bars")
            if _n < 15 and "ATR" in active_indicators:
                _needs.append("ATR: 15 bars")
            if _needs:
                st.warning(
                    f"Only {_n} bars in range — some indicators need more data: "
                    + ", ".join(_needs) + ". Extend the date range for complete signals."
                )
            st.plotly_chart(
                build_detail_fig(df_ohlcv, selected_symbol, active_indicators),
                use_container_width=True,
                theme=None,
            )

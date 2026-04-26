"""
Market Atlas — Streamlit dashboard entry point.

Responsibilities of this file:
  - Page config and global CSS
  - Configuration loading (db_url, etc.)
  - Sidebar controls: index selector, date presets, date pickers, color clip, cache
  - Top-level data fetch (treemap DataFrame shared across all views)
  - Session-state tab navigation

Each view's rendering logic lives in its own module:
  src/dashboard/heatmap.py         render_heatmap_tab
  src/dashboard/sector_synopsis.py render_sector_synopsis_tab
  src/dashboard/stock_detail.py    render_stock_detail

Usage:
    streamlit run src/dashboard/app.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.dashboard.data import (  # noqa: E402
    _get_ohlcv_cache,
    _get_session_cache,
    fetch_available_date_bounds,
    get_treemap_data_cached,
)
from src.dashboard.heatmap import render_heatmap_tab  # noqa: E402
from src.dashboard.sector_synopsis import render_sector_synopsis_tab  # noqa: E402
from src.dashboard.stock_detail import render_stock_detail  # noqa: E402


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INDEX_OPTIONS: dict[str, str] = {
    "S&P 500":   "sp500",
    "NASDAQ-100": "nasdaq100",
    "Dow 30":    "dow30",
    "All":       "all",
}

# label → (days_back | None = YTD, display label)
_DATE_PRESETS: dict[str, tuple[int | None, str]] = {
    "3M":  (90,   "Past 3 months"),
    "6M":  (182,  "Past 6 months"),
    "1Y":  (365,  "Past 1 year"),
    "2Y":  (730,  "Past 2 years"),
    "YTD": (None, "YTD"),
}


# ---------------------------------------------------------------------------
# Callbacks (module-level so Streamlit can reference them stably)
# ---------------------------------------------------------------------------

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
    on_click callback for quick-range preset buttons.
    Runs before the rerun so the active button highlights on the first click.
    """
    d_from, d_to = _preset_dates(label)
    st.session_state["active_preset"] = label
    st.session_state["date_from"] = max(min_day, min(d_from, end_max_day))
    st.session_state["date_to"]   = max(min_day, min(d_to,   end_max_day))


def _on_tab_click(tab_name: str) -> None:
    """on_click callback for custom tab navigation buttons."""
    st.session_state["active_tab"] = tab_name


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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
    db_url = (cfg.get("db_url") or "").strip()

    if not db_url:
        st.error(
            "Missing database configuration. Please set 'db_url' in "
            "src/config/configuration.json (or src/config/config.json)."
        )
        st.stop()

    # -----------------------------------------------------------------------
    # Sidebar — index universe
    # -----------------------------------------------------------------------
    st.sidebar.header("Filters")

    index_label = st.sidebar.selectbox(
        "Index universe", list(INDEX_OPTIONS.keys()), index=3
    )
    index_key = INDEX_OPTIONS[index_label]

    # -----------------------------------------------------------------------
    # Sidebar — date bounds (from DB)
    # -----------------------------------------------------------------------
    min_day, max_day = fetch_available_date_bounds(db_url=db_url, index_key=index_key)
    if not min_day or not max_day:
        st.warning("No price data found for this universe yet.")
        st.stop()

    end_max_day = max(min_day, max_day)

    # -----------------------------------------------------------------------
    # Sidebar — quick-range preset buttons
    # -----------------------------------------------------------------------
    if "active_preset" not in st.session_state:
        st.session_state["active_preset"] = "3M"

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

    # -----------------------------------------------------------------------
    # Sidebar — date pickers
    # -----------------------------------------------------------------------

    # Seed on first load
    if not isinstance(st.session_state.get("date_from"), dt.date):
        d_from, d_to = _preset_dates(st.session_state["active_preset"])
        st.session_state["date_from"] = max(min_day, min(d_from, end_max_day))
        st.session_state["date_to"]   = max(min_day, min(d_to,   end_max_day))

    # Clamp to available bounds; pass as value= (not key=) so preset callbacks
    # always win over any stale widget-internal state.
    _cur_from = max(min_day, min(st.session_state["date_from"], end_max_day))
    _cur_to   = max(min_day, min(st.session_state["date_to"],   end_max_day))

    st.sidebar.subheader("Date range")
    date_from = st.sidebar.date_input(
        "Start date",
        value=_cur_from,
        min_value=min_day,
        max_value=max_day,
        help=f"Available data: {min_day} to {max_day}",
    )
    date_to = st.sidebar.date_input(
        "End date",
        value=_cur_to,
        min_value=min_day,
        max_value=end_max_day,
        help=f"Available data: {min_day} to {end_max_day}",
    )

    # Detect manual date edits → clear preset highlight
    if date_from != _cur_from or date_to != _cur_to:
        st.session_state["active_preset"] = None

    # Sync session_state for the next rerun and for callbacks
    st.session_state["date_from"] = date_from
    st.session_state["date_to"]   = date_to

    if date_from > date_to:
        st.error("Start date must be ≤ end date.")
        st.stop()

    # Clamp queried range to available data (defensive; usually a no-op)
    clamped_from = max(min_day, min(date_from, max_day))
    clamped_to   = max(min_day, min(date_to,   end_max_day))
    if (clamped_from, clamped_to) != (date_from, date_to):
        st.info(f"Clamped date range to available data: {min_day} to {max_day}.")
        date_from, date_to = clamped_from, clamped_to
        st.session_state["date_from"] = date_from
        st.session_state["date_to"]   = date_to

    # Human-readable label used in KPI headings
    _active_preset = st.session_state.get("active_preset")
    if _active_preset and _active_preset in _DATE_PRESETS:
        range_label = _DATE_PRESETS[_active_preset][1]
    else:
        days_span   = (date_to - date_from).days + 1
        range_label = f"{days_span}d ({date_from.isoformat()} → {date_to.isoformat()})"

    # -----------------------------------------------------------------------
    # Sidebar — color scaling
    # -----------------------------------------------------------------------
    st.sidebar.subheader("Color scaling")
    clip        = st.sidebar.slider("Clip return % to ±X", min_value=1, max_value=50, value=10)
    color_range = (-float(clip), float(clip))

    # -----------------------------------------------------------------------
    # Sidebar — cache stats + clear
    # -----------------------------------------------------------------------
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
        st.session_state["treemap_cache_hits"]   = 0
        st.session_state["treemap_cache_misses"] = 0
        st.sidebar.success("Cleared cache")

    # -----------------------------------------------------------------------
    # Data fetch (shared across all views)
    # -----------------------------------------------------------------------
    with st.spinner("Loading data..."):
        df = get_treemap_data_cached(
            db_url=db_url,
            index_key=index_key,
            date_from=date_from,
            date_to=date_to,
            cache_size=cache_size,
        )

    if df.empty:
        st.warning("No data returned for this range/universe. Try expanding the date range.")
        st.stop()

    # -----------------------------------------------------------------------
    # Tab navigation
    # Session-state buttons replace st.tabs, which resets to tab 0 on every
    # full page rerun in Streamlit 1.53 (key= parameter not supported).
    # -----------------------------------------------------------------------
    _TABS = ["Heatmap", "Sector Synopsis", "Stock Detail"]
    if "active_tab" not in st.session_state:
        st.session_state["active_tab"] = "Heatmap"

    _tab_cols = st.columns(len(_TABS))
    for _col, _tab_name in zip(_tab_cols, _TABS):
        _col.button(
            _tab_name,
            key=f"tab_nav_{_tab_name}",
            type="primary" if st.session_state["active_tab"] == _tab_name else "secondary",
            use_container_width=True,
            on_click=_on_tab_click,
            kwargs={"tab_name": _tab_name},
        )

    st.divider()

    # -----------------------------------------------------------------------
    # Active view
    # -----------------------------------------------------------------------
    _active_tab = st.session_state["active_tab"]

    if _active_tab == "Heatmap":
        render_heatmap_tab(df, color_range, range_label)

    elif _active_tab == "Sector Synopsis":
        render_sector_synopsis_tab(df, range_label, db_url, date_from, date_to)

    elif _active_tab == "Stock Detail":
        render_stock_detail(df, db_url, date_from, date_to)


if __name__ == "__main__":
    main()

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
import streamlit.components.v1 as st_components

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
from src.dashboard.index_overlap import render_index_overlap_tab  # noqa: E402
from src.dashboard.news import render_news_tab  # noqa: E402
from src.dashboard.prefs import load_prefs, save_prefs  # noqa: E402
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

# label → internal key passed to render_heatmap_tab
_SIZE_BY_OPTIONS: dict[str, str] = {
    "Dollar volume": "dollar_volume",
    "Equal weight":  "equal_weight",
    "Magnitude":     "magnitude",
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
    # Track that the user has interacted with the tab strip at least once.
    # The mobile tab-reveal animation injects only on the rerun *after* the
    # first such click, by which time Streamlit's initial reconciliation has
    # finished and the columns DOM is stable enough to animate.
    st.session_state["tab_clicked_once"] = True


# ---------------------------------------------------------------------------
# Pref seeding — runs once per browser session
# ---------------------------------------------------------------------------

_VALID_INDEX_LABELS   = set(INDEX_OPTIONS.keys())
_VALID_SIZE_BY_LABELS = set(_SIZE_BY_OPTIONS.keys())
_VALID_PRESET_LABELS  = set(_DATE_PRESETS.keys())
_VALID_PALETTE_LABELS = {"Finviz-style (default)", "RdYlGn", "RdBu (colorblind-safe)", "Viridis (sequential)"}
_VALID_INDICATORS     = {"SMA 20", "SMA 50", "EMA 20", "Bollinger Bands", "RSI", "MACD", "ATR", "OBV"}


def _seed_prefs_once() -> None:
    """
    On the very first run of a browser session, read ~/.marketatlas/prefs.json
    and pre-populate session_state with validated values.  Subsequent reruns
    skip this so in-session widget changes are never overridden.
    """
    if st.session_state.get("_prefs_seeded"):
        return
    p = load_prefs()

    if p.get("index") in _VALID_INDEX_LABELS:
        st.session_state.setdefault("sidebar_index", p["index"])
    else:
        st.session_state.setdefault("sidebar_index", "All")

    if p.get("size_by") in _VALID_SIZE_BY_LABELS:
        st.session_state.setdefault("treemap_size_by", p["size_by"])

    if p.get("default_preset") in _VALID_PRESET_LABELS:
        st.session_state.setdefault("active_preset", p["default_preset"])

    if p.get("palette") in _VALID_PALETTE_LABELS:
        st.session_state.setdefault("color_palette", p["palette"])

    if isinstance(p.get("indicators"), list):
        valid = [i for i in p["indicators"] if i in _VALID_INDICATORS]
        st.session_state.setdefault("detail_indicators", valid)

    st.session_state["_prefs_seeded"] = True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Market Atlas", layout="wide")
    st.markdown(
        """
        <style>
        .block-container { padding-top: 1rem; padding-bottom: 0.5rem; }
        .stMetric { padding: 0.25rem 0.5rem; }
        .stSidebar .block-container { padding-top: 0.75rem; }

        /* ---------- Mobile (≤640px) ---------- */
        @media (max-width: 640px) {
          /* Pin title into the 60px Streamlit header on mobile.
             Subtitle is injected as ::after so both render together inside
             the fixed h1 — page content (tabs) starts right after the header. */
          h1 {
            position: fixed !important;
            /* Vertically centered on the >> sidebar-toggle button (whose
               center sits ~30px from the top of the 60px-tall header). */
            top: 0.85rem !important;
            left: 3rem !important;
            font-size: 1.1rem !important;
            line-height: 1.2 !important;
            margin: 0 !important;
            padding: 0 !important;
            z-index: 999990;
            max-width: calc(100vw - 7rem);
          }
          h1::after {
            content: "Interactive Market Intelligence Dashboard";
            display: block;
            font-size: 0.6rem;
            line-height: 1.1;
            font-weight: 400;
            opacity: 0.65;
            margin-top: 0.1rem;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
          }
          /* Hide the real caption on mobile — its text now lives in h1::after. */
          [data-testid="stElementContainer"]:has(h1)
            + [data-testid="stElementContainer"]:has([data-testid="stCaptionContainer"]) {
            display: none !important;
          }
          /* Content starts right below the title + subtitle stack. */
          .block-container {
            padding-top: 3rem !important;
            padding-bottom: 0.5rem !important;
          }
          /* Tighten the gap between every vertical block on mobile so the
             whole page feels more compact. Default is ~1rem between siblings. */
          [data-testid="stVerticalBlock"] {
            gap: 0.5rem !important;
          }
          /* Movers strip captions ("Top 5 ▲" / "Top 5 ▼") were taking a full
             line-height of margin above and below. */
          [data-testid="stCaptionContainer"] p {
            margin-top: 0 !important;
            margin-bottom: 0 !important;
          }

          /* Tab nav: keep all 5 buttons in one horizontally-scrollable row.
             The marker div lives inside an stElementContainer; the columns row
             is rendered as the immediately-following stLayoutWrapper. */
          [data-testid="stElementContainer"]:has(div[data-tab-nav="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stHorizontalBlock"] {
            flex-wrap: nowrap !important;
            overflow-x: auto;
            scrollbar-width: none;
          }
          /* Mount-time hint: translate every tab column left in unison so the
             rightmost tabs (News + Index Overlap) start visible in the strip's
             viewport, then ease back to 0 so the row settles at its normal
             left-aligned position. Pure CSS — survives Streamlit re-renders. */
          /* Mobile tab-reveal: when the body carries the .tab-hint-trigger
             class (set after the user's first tab click), every tab column
             plays a slide-in from the left so the rightmost tabs (News +
             Index Overlap) are visible during the delay, then the row eases
             back to its normal left-aligned resting position. */
          body.tab-hint-trigger
            [data-testid="stElementContainer"]:has(div[data-tab-nav="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stHorizontalBlock"] > div {
            animation: tab-reveal 1.6s cubic-bezier(0.22, 0.61, 0.36, 1) 0.6s backwards;
          }
          [data-testid="stElementContainer"]:has(div[data-tab-nav="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stHorizontalBlock"]::-webkit-scrollbar {
            display: none;
          }
          [data-testid="stElementContainer"]:has(div[data-tab-nav="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stHorizontalBlock"] > div {
            flex: 0 0 auto !important;
            min-width: 7rem !important;
            width: auto !important;
          }

          /* Heatmap movers strip: fit all 5 cards in one row, no scroll.
             align-items: flex-start prevents Streamlit's column flex from
             stretching cards to equal heights — important because the
             second row otherwise inherits extra vertical space from its
             parent block and the colored border-left visibly elongates. */
          [data-testid="stElementContainer"]:has(div[data-mover-strip="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stHorizontalBlock"] {
            flex-wrap: nowrap !important;
            gap: 0.25rem !important;
            overflow: visible !important;
            align-items: flex-start !important;
          }
          [data-testid="stElementContainer"]:has(div[data-mover-strip="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stHorizontalBlock"] > div {
            flex: 1 1 0 !important;
            min-width: 0 !important;
            width: auto !important;
            align-self: flex-start !important;
            height: auto !important;
          }
          /* Streamlit's stMarkdown chain reports a fixed text-line height
             (~17px) regardless of the rendered card (31px), so the cards
             visually overflow downward and any element below (the heatmap
             chart) covers the bottom curve of the red brackets. Force the
             stLayoutWrapper holding each mover row to reserve enough room
             for the actual card height. */
          [data-testid="stElementContainer"]:has(div[data-mover-strip="true"])
            + [data-testid="stLayoutWrapper"] {
            min-height: 36px !important;
            padding-bottom: 16px !important;
          }
          /* Compact each mover card so 5 fit across at 375px width.
             width: 100% so the card fills its column (uniform widths across
             both rows); height: auto + box-sizing keep the colored border
             matched to the text content height, not stretched. */
          [data-testid="stElementContainer"]:has(div[data-mover-strip="true"])
            + [data-testid="stLayoutWrapper"]
            [data-testid="stMarkdownContainer"] > div {
            display: block !important;
            width: 100% !important;
            height: auto !important;
            box-sizing: border-box !important;
            padding: 4px 6px !important;
            border-left-width: 3px !important;
            min-width: 0 !important;
            overflow: hidden !important;
          }
          /* Breathing room between the green row and the "Top 5 ▼" caption.
             Targets the caption that immediately follows the first mover row. */
          [data-testid="stElementContainer"]:has(div[data-mover-strip="true"])
            + [data-testid="stLayoutWrapper"]
            + [data-testid="stElementContainer"]:has([data-testid="stCaptionContainer"]) {
            margin-top: 0.5rem !important;
          }
          [data-testid="stElementContainer"]:has(div[data-mover-strip="true"])
            + [data-testid="stLayoutWrapper"]
            [data-testid="stMarkdownContainer"] > div > div:first-child {
            font-size: 0.58em !important;
            line-height: 1.1 !important;
          }
          [data-testid="stElementContainer"]:has(div[data-mover-strip="true"])
            + [data-testid="stLayoutWrapper"]
            [data-testid="stMarkdownContainer"] > div > div:nth-child(2) {
            font-size: 0.72em !important;
            line-height: 1.15 !important;
            white-space: nowrap !important;
            overflow: hidden !important;
            text-overflow: ellipsis !important;
          }

          /* Heatmap Options expander: keep the 3 controls (View / Sectors /
             Palette) on one line at mobile width. Labels are short enough
             ("Map" / "Table", "Finviz", etc.) to fit in ~33% slots. */
          [data-testid="stElementContainer"]:has(div[data-options-row="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stHorizontalBlock"] {
            flex-wrap: nowrap !important;
            gap: 0.4rem !important;
          }
          [data-testid="stElementContainer"]:has(div[data-options-row="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stHorizontalBlock"] > div {
            min-width: 0 !important;
            margin-bottom: 0 !important;
          }
          /* Stop the radio labels from wrapping letter-by-letter in narrow columns. */
          [data-testid="stElementContainer"]:has(div[data-options-row="true"])
            + [data-testid="stLayoutWrapper"] label[data-baseweb="radio"] {
            white-space: nowrap;
          }

          /* Mobile-only label shortening for the Heatmap Options row.
             Python keeps the long labels (Treemap / Ranked Table / All sectors);
             we hide the original text and inject the short form via ::before so
             desktop sees the full names and mobile sees the compact ones. */
          [data-testid="stElementContainer"]:has(div[data-options-row="true"])
            + [data-testid="stLayoutWrapper"] label[data-baseweb="radio"]:nth-of-type(1) p {
            font-size: 0;
          }
          [data-testid="stElementContainer"]:has(div[data-options-row="true"])
            + [data-testid="stLayoutWrapper"] label[data-baseweb="radio"]:nth-of-type(1) p::before {
            content: "Map";
            font-size: 0.875rem;
          }
          [data-testid="stElementContainer"]:has(div[data-options-row="true"])
            + [data-testid="stLayoutWrapper"] label[data-baseweb="radio"]:nth-of-type(2) p {
            font-size: 0;
          }
          [data-testid="stElementContainer"]:has(div[data-options-row="true"])
            + [data-testid="stLayoutWrapper"] label[data-baseweb="radio"]:nth-of-type(2) p::before {
            content: "Table";
            font-size: 0.875rem;
          }
          /* Palette selectbox: shorten the selected value's display text per
             option. The displayed div has a `value` attribute carrying the
             literal selected option, so we can match each long label and
             inject the short version via ::before. RdYlGn is already short. */
          [data-testid="stElementContainer"]:has(div[data-options-row="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stSelectbox"]
            div[value="Finviz-style (default)"],
          [data-testid="stElementContainer"]:has(div[data-options-row="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stSelectbox"]
            div[value="RdBu (colorblind-safe)"],
          [data-testid="stElementContainer"]:has(div[data-options-row="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stSelectbox"]
            div[value="Viridis (sequential)"] {
            font-size: 0;
          }
          [data-testid="stElementContainer"]:has(div[data-options-row="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stSelectbox"]
            div[value="Finviz-style (default)"]::before {
            content: "Finviz";
            font-size: 0.875rem;
          }
          [data-testid="stElementContainer"]:has(div[data-options-row="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stSelectbox"]
            div[value="RdBu (colorblind-safe)"]::before {
            content: "RdBu";
            font-size: 0.875rem;
          }
          [data-testid="stElementContainer"]:has(div[data-options-row="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stSelectbox"]
            div[value="Viridis (sequential)"]::before {
            content: "Viridis";
            font-size: 0.875rem;
          }

          /* Multiselect placeholder: hide "All sectors" text and show "All".
             The placeholder text node lives at value-container > inner > 2nd div
             inside the BaseWeb select wrapper. */
          [data-testid="stElementContainer"]:has(div[data-options-row="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stMultiSelect"]
            [data-baseweb="select"] > div:first-child > div:first-child > div:nth-child(2) {
            font-size: 0;
          }
          [data-testid="stElementContainer"]:has(div[data-options-row="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stMultiSelect"]
            [data-baseweb="select"] > div:first-child > div:first-child > div:nth-child(2)::before {
            content: "All";
            font-size: 0.875rem;
          }

          /* Sector Synopsis KPI row: 2-column grid.
             flex-basis must subtract the row gap, otherwise two 50% items
             overflow and each ends up on its own line. */
          [data-testid="stElementContainer"]:has(div[data-kpi-row="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stHorizontalBlock"] {
            flex-wrap: wrap !important;
            gap: 0.5rem !important;
          }
          [data-testid="stElementContainer"]:has(div[data-kpi-row="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stHorizontalBlock"] > div {
            flex: 0 0 calc(50% - 0.25rem) !important;
            width: calc(50% - 0.25rem) !important;
            min-width: 0 !important;
          }
          [data-testid="stElementContainer"]:has(div[data-kpi-row="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stMetricValue"] {
            font-size: 1.1rem !important;
          }

          /* Index Overlap headline: 4 metrics on one line at any width. */
          [data-testid="stElementContainer"]:has(div[data-kpi-row-4="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stHorizontalBlock"] {
            flex-wrap: nowrap !important;
            gap: 0.3rem !important;
          }
          [data-testid="stElementContainer"]:has(div[data-kpi-row-4="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stHorizontalBlock"] > div {
            flex: 1 1 0 !important;
            width: auto !important;
            min-width: 0 !important;
          }
          [data-testid="stElementContainer"]:has(div[data-kpi-row-4="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stMetricLabel"] {
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
          }
          /* Pin every label's inner <p> to the same size so all 4 read uniform. */
          [data-testid="stElementContainer"]:has(div[data-kpi-row-4="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stMetricLabel"] p {
            font-size: 0.75rem !important;
            font-weight: 400 !important;
            line-height: 1.2 !important;
          }
          /* First metric ("Unique symbols") wouldn't fit on mobile — swap to
             "Unique" via the hide-and-inject pseudo-element pattern. */
          [data-testid="stElementContainer"]:has(div[data-kpi-row-4="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stHorizontalBlock"]
            > div:nth-child(1) [data-testid="stMetricLabel"] p {
            font-size: 0 !important;
          }
          [data-testid="stElementContainer"]:has(div[data-kpi-row-4="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stHorizontalBlock"]
            > div:nth-child(1) [data-testid="stMetricLabel"] p::before {
            content: "Unique";
            font-size: 0.75rem;
            font-weight: 400;
            line-height: 1.2;
          }
          [data-testid="stElementContainer"]:has(div[data-kpi-row-4="true"])
            + [data-testid="stLayoutWrapper"] [data-testid="stMetricValue"] {
            font-size: 1.1rem !important;
          }
        }

        /* Tab-reveal keyframes — outside @media so the engine registers them. */
        @keyframes tab-reveal {
          0%   { transform: translateX(-260px); }
          100% { transform: translateX(0); }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    _seed_prefs_once()

    st.title("Market Atlas")
    st.caption("Interactive Market Intelligence Dashboard")

    cfg = load_config()
    db_url = (cfg.get("db_url") or "").strip()
    marketaux_token = (cfg.get("marketaux_token") or "").strip()

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
        "Index universe", list(INDEX_OPTIONS.keys()), key="sidebar_index"
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

    # Clamp stored dates to available bounds BEFORE the key= widgets render.
    # (Writing to session_state[key] after the widget renders raises an error.)
    _clamped_from = max(min_day, min(st.session_state["date_from"], end_max_day))
    _clamped_to   = max(min_day, min(st.session_state["date_to"],   end_max_day))
    if _clamped_from != st.session_state["date_from"]:
        st.session_state["date_from"] = _clamped_from
    if _clamped_to != st.session_state["date_to"]:
        st.session_state["date_to"] = _clamped_to

    st.sidebar.subheader("Date range")
    date_from = st.sidebar.date_input(
        "Start date",
        key="date_from",
        min_value=min_day,
        max_value=max_day,
        help=f"Available data: {min_day} to {max_day}",
    )
    date_to = st.sidebar.date_input(
        "End date",
        key="date_to",
        min_value=min_day,
        max_value=end_max_day,
        help=f"Available data: {min_day} to {end_max_day}",
    )

    # Clear preset highlight if the user manually edited either date.
    # Preset button callbacks set active_preset before the rerun, so if
    # the current dates no longer match what that preset would produce,
    # the user must have overridden them manually.
    _active_preset = st.session_state.get("active_preset")
    if _active_preset and _active_preset in _DATE_PRESETS:
        _exp_from, _exp_to = _preset_dates(_active_preset)
        _exp_from = max(min_day, min(_exp_from, end_max_day))
        _exp_to   = max(min_day, min(_exp_to,   end_max_day))
        if date_from != _exp_from or date_to != _exp_to:
            st.session_state["active_preset"] = None

    if date_from > date_to:
        st.error("Start date must be ≤ end date.")
        st.stop()

    # Human-readable label used in KPI headings
    _active_preset = st.session_state.get("active_preset")
    if _active_preset and _active_preset in _DATE_PRESETS:
        range_label = _DATE_PRESETS[_active_preset][1]
    else:
        days_span   = (date_to - date_from).days + 1
        range_label = f"{days_span}d ({date_from.isoformat()} → {date_to.isoformat()})"

    # -----------------------------------------------------------------------
    # Sidebar — heatmap display (hidden; uncomment to re-enable size selector)
    # -----------------------------------------------------------------------
    # st.sidebar.subheader("Heatmap")
    # size_by = _SIZE_BY_OPTIONS[st.sidebar.selectbox(
    #     "Size tiles by",
    #     list(_SIZE_BY_OPTIONS.keys()),
    #     key="treemap_size_by",
    #     help=(
    #         "Dollar volume — tile area ∝ end price × shares traded.\n\n"
    #         "Equal weight — every tile the same size; colour is the only signal.\n\n"
    #         "Magnitude — tile area ∝ |return %|; highlights movers regardless of liquidity."
    #     ),
    # )]
    size_by = "dollar_volume"

    # -----------------------------------------------------------------------
    # Sidebar — cache stats + clear
    # -----------------------------------------------------------------------
    cache_size = 24
    st.sidebar.subheader("Cache")
    # --- Clear cache button (hidden; uncomment to re-enable) ---
    # if st.sidebar.button("Clear cached results"):
    #     _get_session_cache().clear()
    #     _get_ohlcv_cache().clear()
    #     st.session_state["treemap_cache_hits"]   = 0
    #     st.session_state["treemap_cache_misses"] = 0
    #     st.sidebar.success("Cleared cache")
    _tm_hits   = st.session_state.get("treemap_cache_hits",   0)
    _tm_misses = st.session_state.get("treemap_cache_misses", 0)
    _tm_slots  = len(_get_session_cache())
    _ov_slots  = len(_get_ohlcv_cache())
    st.sidebar.caption(
        f"Treemap: {_tm_hits} hits, {_tm_misses} misses "
        f"· {_tm_slots} of {cache_size} slots used"
    )
    st.sidebar.caption(f"OHLCV: {_ov_slots} series cached")

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
    _TABS = ["Heatmap", "Sector Synopsis", "Stock Detail", "News", "Index Overlap"]
    if "active_tab" not in st.session_state:
        st.session_state["active_tab"] = "Heatmap"

    # Marker div lets the mobile CSS target *only* this columns row and turn
    # it into a horizontally scrollable strip instead of stacking 5 buttons
    # vertically on narrow viewports.
    st.markdown('<div data-tab-nav="true"></div>', unsafe_allow_html=True)
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

    # Mobile tab-reveal hint: a CSS class is added to <body> on the rerun
    # *after* the first tab click. The mobile-only CSS rule applies the
    # tab-reveal keyframe animation only when that class is present, so the
    # animation plays once after the user starts interacting (by which time
    # Streamlit's initial reconciliation has settled).
    if st.session_state.get("tab_clicked_once"):
        st_components.html(
            """
            <script>
              (function () {
                const body = window.parent.document.body;
                if (body) body.classList.add('tab-hint-trigger');
              })();
            </script>
            """,
            height=0,
        )

    # -----------------------------------------------------------------------
    # Active view
    # -----------------------------------------------------------------------
    _active_tab = st.session_state["active_tab"]

    if _active_tab == "Heatmap":
        render_heatmap_tab(df, index_key, date_from, date_to, size_by=size_by)

    elif _active_tab == "Sector Synopsis":
        render_sector_synopsis_tab(df, range_label, db_url, date_from, date_to)

    elif _active_tab == "Stock Detail":
        render_stock_detail(df, db_url, date_from, date_to)

    elif _active_tab == "News":
        render_news_tab(df, marketaux_token)

    elif _active_tab == "Index Overlap":
        render_index_overlap_tab(db_url)

    # Persist UI prefs to disk so they survive tab close / refresh.
    save_prefs({
        "index":          st.session_state.get("sidebar_index",    "All"),
        "palette":        st.session_state.get("color_palette",    "Finviz-style (default)"),
        "size_by":        st.session_state.get("treemap_size_by",  "Dollar volume"),
        "indicators":     st.session_state.get("detail_indicators", ["SMA 20", "SMA 50"]),
        "default_preset": st.session_state.get("active_preset",    "3M"),
    })


if __name__ == "__main__":
    main()

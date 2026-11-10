"""
Market Atlas — Streamlit dashboard entry point.

Responsibilities of this file:
  - Page config and the global stylesheet (kept small on purpose)
  - Configuration loading (db_url, etc.)
  - Header, tabs, and the filter row at the top of each tab: index universe,
    time range (incl. Custom dialog), plus the sector filter on the Heatmap
  - Tab navigation (native st.tabs; only the open tab's code runs)
  - Usage-event logging

Each view's rendering logic lives in its own module:
  src/dashboard/heatmap.py         render_heatmap_tab
  src/dashboard/ask.py             render_ask_tab
  src/dashboard/sector_synopsis.py render_sector_synopsis_tab
  src/dashboard/stock_detail.py    render_stock_detail
  src/dashboard/news.py            render_news_tab

Usage:
    streamlit run src/dashboard/app.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo


# Developer-only UI (cache stats + clear buttons in a sidebar) is gated
# behind the same env var that controls Streamlit's full toolbar
# (.streamlit/config.toml has `toolbarMode = "minimal"` for end users):
#
#   STREAMLIT_CLIENT_TOOLBAR_MODE=developer streamlit run src/dashboard/app.py
_DEVELOPER_MODE = os.environ.get("STREAMLIT_CLIENT_TOOLBAR_MODE") == "developer"

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.dashboard.data import (  # noqa: E402
    _get_ohlcv_cache,
    _get_session_cache,
    fetch_available_date_bounds,
    fetch_index_overlap,
    fetch_previous_trading_day,
    get_treemap_data_cached,
)
from src.dashboard.heatmap import render_heatmap_tab  # noqa: E402
from src.dashboard.ask import render_ask_tab  # noqa: E402
from src.dashboard.index_overlap import render_index_overlap_tab  # noqa: E402
from src.dashboard.news import render_news_tab  # noqa: E402
from src.dashboard.sector_synopsis import render_sector_synopsis_tab  # noqa: E402
from src.dashboard.stock_detail import render_stock_detail  # noqa: E402
from src.db.repositories import log_usage_event  # noqa: E402


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """
    Load config via src.config.settings.load_settings(), which checks
    Streamlit secrets → env vars → src/config/configuration.json in priority
    order. Returns an empty dict if nothing is configured, so the caller can
    show a friendly st.error instead of crashing.
    """
    try:
        from dataclasses import asdict
        from src.config.settings import load_settings
        return asdict(load_settings())
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Filter-bar label → index_key. The first entry is the default.
INDEX_OPTIONS: dict[str, str] = {
    "All indexes": "all",
    "S&P 500":     "sp500",
    "NASDAQ-100":  "nasdaq100",
    "Dow 30":      "dow30",
}

# Preset key → (days back, or "prev" = previous trading day;
#               dropdown label; heading label used by the tabs)
_DATE_PRESETS: dict[str, tuple[int | str, str, str]] = {
    "24H": ("prev", "24 hours", "Last trading day"),
    "1W":  (7,      "1 week",   "Past week"),
    "1M":  (30,     "1 month",  "Past month"),
    "3M":  (90,     "3 months", "Past 3 months"),
    "6M":  (182,    "6 months", "Past 6 months"),
    "1Y":  (365,    "1 year",   "Past year"),
}
_DEFAULT_PRESET = "1W"

# Time-range dropdown entries besides the presets.
_CUSTOM_ACTIVE = "custom"      # the applied custom range, shown as its dates
_CUSTOM_NEW = "custom-new"     # "Custom…": opens the date dialog

# Index Overlap is built but hidden; add it back here to re-enable it
# (its render branch is kept below).
_TABS = ["Heatmap", "Ask AI", "Sector Synopsis", "Stock Detail", "News"]

_CSS = """
<style>
/* Streamlit adds "Ask Google" / "Ask ChatGPT" links next to errors; they
   would send internal SQL and error text to third-party sites. */
a[href*="google.com/search"],
a[href*="chatgpt.com/?q="],
a[href*="chat.openai.com/?q="] { display: none !important; }

/* The chat input's character counter sits under the send button. */
[data-testid="InputInstructions"] { display: none !important; }

/* Keep the page steady while a click is processed. By default Streamlit
   fades every element out and back in on each rerun. */
[data-stale="true"] { opacity: 1 !important; transition: none !important; }

/* Streamlit's header bar is empty here (it only shows the "running"
   indicator), so let the page start right under the top edge. */
.block-container { padding-top: 2rem; padding-bottom: 2rem; }
h1 { padding-top: 0 !important; padding-bottom: 0 !important; }

/* Time range: tinted in the accent colour (same as the active tab) so the
   period being shown stands out. */
.st-key-range_choice [role="group"] {
  background: rgba(197, 57, 72, 0.08) !important;
  border-color: rgba(197, 57, 72, 0.55) !important;
  color: #c53948 !important;
}
.st-key-range_choice input {
  color: #c53948 !important;
  -webkit-text-fill-color: #c53948;
  font-weight: 600 !important;
}

/* Heatmap: top movers */
.ma-movers { display: grid; gap: 0.3rem; margin-bottom: 0.25rem; }
.ma-movers-label { font-size: 0.75rem; opacity: 0.65; }
.ma-movers-row {
  display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 0.4rem;
}
.ma-card {
  min-width: 0; border-left: 3px solid var(--c); border-radius: 6px;
  padding: 0.3rem 0.55rem; background: rgba(127, 127, 127, 0.08);
}
.ma-card b {
  display: block; font-size: 0.7rem; letter-spacing: 0.04em; opacity: 0.7;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ma-card .ma-px {
  display: block; font-size: 0.95rem; font-weight: 600; white-space: nowrap;
}
.ma-card .ma-chg {
  display: block; font-size: 0.85rem; font-weight: 700; color: var(--c);
  white-space: nowrap;
}
.ma-card .ma-chg em { font-style: normal; font-weight: 500; margin-left: 0.4rem; }

/* Heatmap: colour key (replaces Plotly's colorbar, which ate a third of
   the width on phones) */
.ma-legend {
  display: flex; align-items: center; gap: 0.5rem;
  font-size: 0.75rem; opacity: 0.75;
}
.ma-legend i {
  flex: 0 1 140px; height: 8px; border-radius: 4px;
  background: linear-gradient(90deg, #a11318, #3a3f42 50%, #26a113);
}

/* Sector Synopsis: headline numbers */
.ma-kpis {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 0.5rem 1.25rem;
}
.ma-kpis small { display: block; font-size: 0.8rem; opacity: 0.7; }
.ma-kpis b { display: block; font-size: 1.6rem; font-weight: 400; line-height: 1.3; }

@media (max-width: 640px) {
  .block-container { padding: 1rem 0.75rem 1.5rem; }
  h1 { font-size: 1.6rem !important; }
  .ma-legend-note { display: none; }
  /* Filter row: sectors on its own line, index + time range share the next. */
  .st-key-filter_row .st-key-heatmap_sector_filter {
    flex: 1 1 100% !important; width: 100% !important; max-width: 100% !important;
  }
  .st-key-filter_row .st-key-index_choice,
  .st-key-filter_row .st-key-range_choice {
    flex: 1 1 0 !important; width: auto !important; min-width: 0 !important;
  }
  .ma-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ma-kpis b { font-size: 1.2rem; }
  .ma-movers-row { gap: 0.25rem; }
  .ma-card { padding: 0.25rem 0.35rem; }
  .ma-card b { font-size: 0.6rem; }
  .ma-card .ma-px { font-size: 0.72rem; }
  .ma-card .ma-chg { font-size: 0.68rem; }
  .ma-card .ma-chg em { display: block; margin-left: 0; }
}
</style>
"""


# ---------------------------------------------------------------------------
# Date range helpers
# ---------------------------------------------------------------------------

def _preset_dates(
    key: str, prev_day: dt.date | None = None
) -> tuple[dt.date, dt.date]:
    """
    Compute (date_from, date_to) for a preset key, anchored to today.
    "24H" starts at `prev_day` (the previous trading day's close) so the
    range spans exactly two closes; callers clamp date_to to the data.
    """
    today = dt.date.today()
    days_back = _DATE_PRESETS[key][0]
    if days_back == "prev":
        return prev_day or today - dt.timedelta(days=1), today
    return today - dt.timedelta(days=days_back), today


def _fmt_span(d_from: dt.date, d_to: dt.date) -> str:
    """'Nov 2 – Nov 6, 2026' (or with both years when they differ)."""
    left = f"{d_from:%b} {d_from.day}"
    if d_from.year != d_to.year:
        left += f", {d_from.year}"
    return f"{left} – {d_to:%b} {d_to.day}, {d_to.year}"


def _freshness_label(latest: dt.date, today: dt.date | None = None) -> str:
    """
    Data-age note for the header, e.g. "Prices as of Fri, Nov 6 close · 3 days ago".

    `today` defaults to the current date in US/Eastern (the market's
    timezone), so the age doesn't jump a day early on a UTC server.
    """
    if today is None:
        today = dt.datetime.now(ZoneInfo("America/New_York")).date()
    age = (today - latest).days
    if age <= 0:
        rel = "today"
    elif age == 1:
        rel = "yesterday"
    else:
        rel = f"{age} days ago"
    return f"Prices as of {latest:%a, %b} {latest.day} close · {rel}"


def _resolve_range(
    min_day: dt.date, end_max_day: dt.date, prev_day: dt.date | None
) -> tuple[dt.date, dt.date, str]:
    """Current (date_from, date_to, heading label), clamped to the data."""
    preset = st.session_state.get("range_preset", _DEFAULT_PRESET)
    if preset in _DATE_PRESETS:
        d_from, d_to = _preset_dates(preset, prev_day)
        label = _DATE_PRESETS[preset][2]
    else:
        d_from, d_to = st.session_state.get(
            "custom_range", _preset_dates(_DEFAULT_PRESET)
        )
        label = ""

    def clamp(d: dt.date) -> dt.date:
        return max(min_day, min(d, end_max_day))

    d_from, d_to = clamp(d_from), clamp(d_to)
    return d_from, d_to, label or _fmt_span(d_from, d_to)


def _current_range_choice() -> str:
    preset = st.session_state.get("range_preset", _DEFAULT_PRESET)
    return preset if preset in _DATE_PRESETS else _CUSTOM_ACTIVE


def _on_range_change() -> None:
    """on_change for the time-range dropdown (runs before the rerun)."""
    choice = st.session_state.get("range_choice")
    if choice in _DATE_PRESETS:
        st.session_state["range_preset"] = choice
    elif choice == _CUSTOM_NEW:
        st.session_state["_custom_dialog_open"] = True


def _on_custom_dismiss() -> None:
    """Custom dialog closed without applying: show the active range again."""
    st.session_state["range_choice"] = _current_range_choice()


@st.dialog("Custom range", on_dismiss=_on_custom_dismiss)
def _custom_range_dialog(
    min_day: dt.date, end_max_day: dt.date, current: tuple[dt.date, dt.date]
) -> None:
    """Pick a start date, then an end date; the range applies on the second pick."""
    picked = st.date_input(
        "Pick a start date, then an end date",
        value=current,
        min_value=min_day,
        max_value=end_max_day,
        format="MM/DD/YYYY",
    )
    st.caption(
        f"Prices are available from {_fmt_span(min_day, end_max_day)}."
    )
    if (
        isinstance(picked, (tuple, list))
        and len(picked) == 2
        and tuple(picked) != tuple(current)
    ):
        # Plain session keys (no widget owns them), so they can be set
        # here; the full rerun also closes the dialog.
        st.session_state["range_preset"] = None
        st.session_state["custom_range"] = (picked[0], picked[1])
        st.rerun()


# ---------------------------------------------------------------------------
# Usage logging
# ---------------------------------------------------------------------------

def _get_session_device() -> tuple[str | None, bool | None]:
    """
    Read the browser User-Agent once per session from st.context.headers
    and derive a coarse mobile/desktop flag. Cached in session_state.
    Returns (user_agent, is_mobile); either may be None.
    """
    if "_device_classified" in st.session_state:
        return (
            st.session_state.get("_device_user_agent"),
            st.session_state.get("_device_is_mobile"),
        )

    user_agent: str | None = None
    is_mobile: bool | None = None
    try:
        headers = getattr(st.context, "headers", None)
        if headers is not None:
            user_agent = headers.get("User-Agent") or headers.get("user-agent")
    except Exception:
        user_agent = None

    if user_agent:
        # iPad is intentionally NOT counted as mobile: Apple's UA reports
        # it as desktop and viewport-wise it behaves more like one.
        ua_lower = user_agent.lower()
        is_mobile = any(
            marker in ua_lower
            for marker in ("mobile", "android", "iphone", "ipod")
        )

    st.session_state["_device_user_agent"] = user_agent
    st.session_state["_device_is_mobile"] = is_mobile
    st.session_state["_device_classified"] = True
    return user_agent, is_mobile


def _on_tab_change(db_url: str = "") -> None:
    """on_change for st.tabs: log the switch to usage_events."""
    prev = st.session_state.get("_last_tab", _TABS[0])
    new = st.session_state.get("active_tab", _TABS[0])
    st.session_state["_last_tab"] = new
    print(
        f"[app] {dt.datetime.now().isoformat(timespec='seconds')} "
        f"tab change: {prev!r} → {new!r}",
        file=sys.stderr,
        flush=True,
    )
    if db_url:
        ua, is_mobile = _get_session_device()
        log_usage_event(
            db_url,
            session_id=st.session_state.get("_session_id", "unknown"),
            event_type="tab_change",
            from_tab=prev,
            to_tab=new,
            user_agent=ua,
            is_mobile=is_mobile,
        )


# ---------------------------------------------------------------------------
# Page sections
# ---------------------------------------------------------------------------

def _sector_options(db_url: str, index_key: str) -> list[str]:
    """Sectors present in the selected universe (from the cached membership data)."""
    members = fetch_index_overlap(db_url)
    if index_key != "all":
        members = members[members[f"in_{index_key}"]]
    return sorted(members["sector"].dropna().unique().tolist())


def _render_filter_row(span_label: str, sector_options: list[str] | None = None) -> None:
    """
    One row at the top of each tab except Ask AI: the Heatmap's sector
    filter (when `sector_options` is given), then index universe and time
    range. Index and time range apply to every tab that shows them.
    """
    options = list(_DATE_PRESETS)
    if _current_range_choice() == _CUSTOM_ACTIVE:
        options.append(_CUSTOM_ACTIVE)
    options.append(_CUSTOM_NEW)

    # Keep the dropdown on the active range — except on the run that opens
    # the Custom dialog, where it should keep showing "Custom…".
    if not st.session_state.get("_custom_dialog_open"):
        st.session_state["range_choice"] = _current_range_choice()

    def fmt(opt: str) -> str:
        if opt == _CUSTOM_ACTIVE:
            return span_label
        if opt == _CUSTOM_NEW:
            return "Custom…"
        return _DATE_PRESETS[opt][1]

    with st.container(horizontal=True, gap="small", key="filter_row"):
        if sector_options is not None:
            # Drop selections that aren't in this universe (e.g. after
            # switching index) before the widget validates them.
            stored = st.session_state.get("heatmap_sector_filter", [])
            kept = [s for s in stored if s in sector_options]
            if kept != stored:
                st.session_state["heatmap_sector_filter"] = kept
            st.multiselect(
                "Sectors",
                options=sector_options,
                key="heatmap_sector_filter",
                placeholder="All sectors",
                label_visibility="collapsed",
                width=460,
            )
        st.selectbox(
            "Index",
            list(INDEX_OPTIONS),
            key="index_choice",
            label_visibility="collapsed",
            filter_mode=None,   # no typing, so phones don't open a keyboard
            width=200,
        )
        st.selectbox(
            "Time range",
            options,
            key="range_choice",
            format_func=fmt,
            on_change=_on_range_change,
            label_visibility="collapsed",
            filter_mode=None,
            width=240,
        )


def _render_dev_sidebar() -> None:
    """Cache stats and clear button; developer mode only."""
    with st.sidebar:
        st.subheader("Cache")
        if st.button("Clear cached results"):
            _get_session_cache().clear()
            _get_ohlcv_cache().clear()
            st.session_state["treemap_cache_hits"] = 0
            st.session_state["treemap_cache_misses"] = 0
            st.success("Cleared cache")
        hits = st.session_state.get("treemap_cache_hits", 0)
        misses = st.session_state.get("treemap_cache_misses", 0)
        st.caption(
            f"Treemap: {hits} hits, {misses} misses "
            f"· {len(_get_session_cache())} of 24 slots used"
        )
        st.caption(f"OHLCV: {len(_get_ohlcv_cache())} series cached")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Market Atlas", layout="wide")
    st.markdown(_CSS, unsafe_allow_html=True)

    # One stable id per browser session, reused for every usage_events row.
    if not st.session_state.get("_app_loaded_logged"):
        st.session_state["_session_id"] = str(uuid.uuid4())
        print(
            f"[app] {dt.datetime.now().isoformat(timespec='seconds')} "
            f"new session loaded — Streamlit started serving Market Atlas",
            file=sys.stderr,
            flush=True,
        )
        st.session_state["_app_loaded_logged"] = True

    cfg = load_config()
    db_url = (cfg.get("db_url") or "").strip()
    marketaux_token = (cfg.get("marketaux_token") or "").strip()
    db_url_readonly = (cfg.get("db_url_readonly") or "").strip()
    anthropic_api_key = (cfg.get("anthropic_api_key") or "").strip()
    anthropic_model = (cfg.get("anthropic_model") or "").strip() or "claude-haiku-4-5"

    if not db_url:
        st.title("Market Atlas")
        st.error(
            "Missing database configuration. Please set 'db_url' in "
            "src/config/configuration.json (or src/config/config.json)."
        )
        st.stop()

    # Persist a 'session_load' usage event once per browser session.
    # Best-effort: log_usage_event swallows errors and runs on a thread.
    if not st.session_state.get("_app_load_persisted"):
        ua, is_mobile = _get_session_device()
        log_usage_event(
            db_url,
            session_id=st.session_state.get("_session_id", "unknown"),
            event_type="session_load",
            to_tab=_TABS[0],
            user_agent=ua,
            is_mobile=is_mobile,
        )
        st.session_state["_app_load_persisted"] = True

    # The index dropdown renders below the header, but its value is already
    # in session_state at the start of the run, so the header can show how
    # fresh the selected universe's prices are.
    index_label = st.session_state.get("index_choice", next(iter(INDEX_OPTIONS)))
    index_key = INDEX_OPTIONS.get(index_label, "all")
    min_day, max_day = fetch_available_date_bounds(db_url=db_url, index_key=index_key)

    st.title("Market Atlas", anchor=False)
    if not min_day or not max_day:
        st.warning("No price data found for this universe yet.")
        st.stop()
    st.caption(f"AI-Powered Market Intelligence Web App · {_freshness_label(max_day)}")

    end_max_day = max(min_day, max_day)
    prev_day = fetch_previous_trading_day(db_url, max_day)
    st.session_state.setdefault("range_preset", _DEFAULT_PRESET)
    date_from, date_to, range_label = _resolve_range(min_day, end_max_day, prev_day)

    span_label = _fmt_span(date_from, date_to)

    # Some filters aren't drawn on every tab (sectors only on the Heatmap,
    # none on Ask AI). Streamlit drops a widget's value on runs where it
    # isn't drawn; re-assigning it here keeps the selection across tabs.
    for key in ("index_choice", "heatmap_sector_filter"):
        if key in st.session_state:
            st.session_state[key] = st.session_state[key]

    if _DEVELOPER_MODE:
        _render_dev_sidebar()

    # Tabs: only the open tab's code runs (on_change makes them lazy), so
    # e.g. Ask AI never waits on the treemap query.
    tabs = st.tabs(
        _TABS,
        key="active_tab",
        default=_TABS[0],
        on_change=_on_tab_change,
        kwargs={"db_url": db_url},
    )

    def load_prices():
        with st.spinner("Loading prices…"):
            df = get_treemap_data_cached(
                db_url=db_url,
                index_key=index_key,
                date_from=date_from,
                date_to=date_to,
                cache_size=24,
            )
        if df.empty:
            st.warning("No prices for this range. Try a longer time range.")
            return None
        return df

    for name, tab in zip(_TABS, tabs):
        if not tab.open:
            continue
        with tab:
            # Filter row first, so it stays put while the data loads below it.
            # Ask AI has none: questions carry their own index and dates.
            if name != "Ask AI":
                _render_filter_row(
                    span_label,
                    _sector_options(db_url, index_key) if name == "Heatmap" else None,
                )

            if name == "Ask AI":
                ai_client = None
                if anthropic_api_key:
                    from src.ai.client import AIClient
                    try:
                        ai_client = AIClient(api_key=anthropic_api_key, model=anthropic_model)
                    except ValueError:
                        ai_client = None
                render_ask_tab(
                    ai_client=ai_client,
                    db_url_readonly=db_url_readonly,
                    db_url=db_url,
                )
                continue

            if name == "Index Overlap":
                render_index_overlap_tab(db_url)
                continue

            df = load_prices()
            if df is None:
                continue
            if name == "Heatmap":
                render_heatmap_tab(
                    df, index_key, date_from, date_to,
                    sectors=st.session_state.get("heatmap_sector_filter", []),
                    # Phones can't hover, so print price + return on the tiles.
                    tile_values=bool(_get_session_device()[1]),
                )
            elif name == "Sector Synopsis":
                render_sector_synopsis_tab(df, range_label, db_url, date_from, date_to)
            elif name == "Stock Detail":
                render_stock_detail(df, db_url, date_from, date_to)
            elif name == "News":
                render_news_tab(df, marketaux_token)

    # Opened outside the tab containers; "Custom…" in the time-range
    # dropdown sets the flag in its on_change callback.
    if st.session_state.pop("_custom_dialog_open", False):
        _custom_range_dialog(min_day, end_max_day, (date_from, date_to))


if __name__ == "__main__":
    main()

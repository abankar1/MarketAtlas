"""
Heatmap tab — treemap and ranked-table views.

render_heatmap_tab(df, index_key, date_from, date_to)
    renders the full tab: sector filter, color controls, movers strip, treemap or ranked table.
render_ranked_table(df, color_range)
    ranked table helper (symbol, name, sector, return %, percentile, volume).
"""

from __future__ import annotations

import datetime as dt
import io
import re

import pandas as pd
import plotly.colors as pc
import streamlit as st

from src.dashboard.charts import build_fig


# ---------------------------------------------------------------------------
# Ranked table
# ---------------------------------------------------------------------------


def _cell_t(val: float, vmin: float, vmax: float, midpoint: float) -> float:
    """
    Map val → [0, 1] with `midpoint` at 0.5 for diverging scales.
    Below midpoint: linear [0 → 0.5]; above: linear [0.5 → 1].
    """
    if val <= midpoint:
        span = midpoint - vmin
        return max(0.0, 0.5 * (val - vmin) / span) if span else 0.0
    else:
        span = vmax - midpoint
        return min(1.0, 0.5 + 0.5 * (val - midpoint) / span) if span else 1.0


def _contrast(rgb_str: str) -> str:
    """Return 'black' or 'white' for readable text on the given RGB background."""
    nums = [int(x) for x in re.findall(r"\d+", rgb_str)]
    if len(nums) >= 3:
        lum = (0.299 * nums[0] + 0.587 * nums[1] + 0.114 * nums[2]) / 255
        return "black" if lum > 0.45 else "white"
    return "black"


_SEQUENTIAL_SCALES = {"Viridis", "Plasma", "Inferno", "Magma", "Cividis"}

# Finviz-style palette — dark red → dark grey → dark green, no yellow.
# Quieter and more readable on a dark background than RdYlGn.
_FINVIZ_LIKE: list[list] = [
    [0.00, "#a11318"],
    [0.25, "#82181c"],
    [0.45, "#5a2c30"],
    [0.50, "#3a3f42"],
    [0.55, "#2c5325"],
    [0.75, "#287a1a"],
    [1.00, "#26a113"],
]

# label → Plotly colorscale (named string OR custom list of stops)
_PALETTE_OPTIONS: dict[str, object] = {
    "Finviz-style (default)": _FINVIZ_LIKE,
    "RdYlGn":                 "RdYlGn",
    "RdBu (colorblind-safe)": "RdBu",
    "Viridis (sequential)":   "Viridis",
}


def render_ranked_table(
    df: pd.DataFrame,
    color_range: tuple[float, float],
    color_scale: str = "RdYlGn",
    center_zero: bool = True,
) -> None:
    """
    Sorted table of all symbols: rank, symbol, name, sector, return %,
    percentile, dollar volume, volume, start/end close.

    Return % cells are background-colored with the same palette and
    midpoint settings as the treemap.
    Percentile column shows a CSS bar from red (0) to green (100).
    """
    tdf = df[
        [
            "symbol",
            "name",
            "group_name",
            "return_pct",
            "dollar_volume",
            "end_volume",
            "start_close",
            "end_close",
        ]
    ].copy()

    tdf["percentile_rank"] = tdf["return_pct"].rank(ascending=True) / len(tdf) * 100
    tdf = tdf.sort_values("return_pct", ascending=False).reset_index(drop=True)
    tdf.index = tdf.index + 1  # 1-based rank

    tdf = tdf.rename(
        columns={
            "symbol": "Symbol",
            "name": "Name",
            "group_name": "Sector",
            "return_pct": "Return %",
            "percentile_rank": "Percentile",
            "dollar_volume": "Dollar Volume",
            "end_volume": "Volume",
            "start_close": "Start Close",
            "end_close": "End Close",
        }
    )

    # Color "Return %" cells by percentile rank — same logic as the treemap
    def _return_cell_style_by_pct(percentile: float) -> str:
        t = max(0.0, min(1.0, percentile / 100.0))
        rgb = pc.sample_colorscale(color_scale, [t])[0]
        return f"background-color: {rgb}; color: {_contrast(rgb)}"

    # Build a styler keyed off the Percentile column for the Return % cells
    pct_styles = tdf["Percentile"].apply(_return_cell_style_by_pct)
    return_cell_styles = pd.DataFrame(
        {"Return %": pct_styles.values}, index=tdf.index
    )

    styled = (
        tdf.style.apply(lambda _: return_cell_styles, axis=None)
        .bar(subset=["Percentile"], color=["#ef5350", "#26a69a"], vmin=0, vmax=100)
        .format(
            {
                "Return %": "{:+.2f}%",
                "Percentile": "{:.0f}",
                "Dollar Volume": "${:,.0f}",
                "Volume": "{:,.0f}",
                "Start Close": "${:.2f}",
                "End Close": "${:.2f}",
            }
        )
    )

    st.caption(
        f"{len(tdf)} symbols · sortable by any column · "
        "color scale matches the treemap"
    )
    st.dataframe(styled, use_container_width=True)


# ---------------------------------------------------------------------------
# Date-range guard
# ---------------------------------------------------------------------------


def _exceeds_three_months(d_from: dt.date, d_to: dt.date) -> bool:
    """
    Return True if d_to is strictly more than 3 calendar months after d_from.

    Examples:
        Jan 22 → Apr 22  →  False  (exactly 3 months, allowed)
        Jan 22 → Apr 23  →  True   (over limit)
        Jan 31 → Apr 30  →  False  (3-month cutoff clamped to last day of April)
    """
    target_month = d_from.month + 3
    target_year = d_from.year + (target_month - 1) // 12
    target_month = (target_month - 1) % 12 + 1
    try:
        cutoff = d_from.replace(year=target_year, month=target_month)
    except ValueError:
        # day overshoots month-end (e.g. Jan 31 → Apr 31 → clamp to Apr 30)
        cutoff = dt.date(target_year, target_month + 1, 1) - dt.timedelta(days=1)
    return d_to > cutoff


# ---------------------------------------------------------------------------
# CSV export helper
# ---------------------------------------------------------------------------


def _build_export_csv(df: pd.DataFrame) -> bytes:
    """
    Build the heatmap export CSV (in-memory).

    Columns: symbol, name, sector, return_pct, dollar_volume, percentile_rank.
    Sorted best → worst. percentile_rank is 0–100 rounded to 1 decimal.
    """
    export = df[["symbol", "name", "group_name", "return_pct", "dollar_volume"]].copy()
    export["percentile_rank"] = (
        export["return_pct"].rank(ascending=True) / len(export) * 100
    ).round(1)
    export = export.sort_values("return_pct", ascending=False)
    export = export.rename(columns={"group_name": "sector"})
    buf = io.StringIO()
    export.to_csv(buf, index=False)
    return buf.getvalue().encode()


# ---------------------------------------------------------------------------
# Top movers strip
# ---------------------------------------------------------------------------


def _render_movers_strip(
    df: pd.DataFrame,
    color_range: tuple[float, float],  # retained for API compat; ignored
    color_scale: str,
    center_zero: bool,                   # retained for API compat; ignored
) -> None:
    """
    Compact two-row strip: top-N gainers (▲) and top-N losers (▼).

    Each cell is a small card with a colored left-border chip, the ticker,
    and the return % rendered in the palette colour matching the treemap
    (percentile-rank within the visible df). N = min(5, len(df)).
    """
    n = min(5, len(df))
    if n == 0:
        return

    # Percentile rank — matches the treemap's color logic exactly
    if len(df) > 1:
        ranks = df["return_pct"].rank(method="average", pct=True) * 100
    else:
        ranks = pd.Series([50.0] * len(df), index=df.index)
    pct_by_symbol = dict(zip(df["symbol"], ranks))

    def _color(percentile: float) -> str:
        t = max(0.0, min(1.0, percentile / 100.0))
        return pc.sample_colorscale(color_scale, [t])[0]

    def _row(label: str, subset: pd.DataFrame) -> None:
        st.caption(label)
        # Marker so the mobile CSS can keep all 5 cards in one
        # horizontally-scrollable row instead of stacking them.
        st.markdown('<div data-mover-strip="true"></div>', unsafe_allow_html=True)
        cols = st.columns(n)
        for col, (_, row) in zip(cols, subset.iterrows()):
            pct = row["return_pct"]
            color = _color(pct_by_symbol.get(row["symbol"], 50.0))
            sign = "+" if pct >= 0 else ""
            col.markdown(
                f'<div style="'
                f"padding:6px 10px;"
                f"border-radius:6px;"
                f"border-left:4px solid {color};"
                f"background:rgba(255,255,255,0.04);"
                f'margin-bottom:2px">'
                f'<div style="font-size:0.70em;color:#aaa;'
                f'letter-spacing:0.05em;font-weight:600">'
                f'{row["symbol"]}</div>'
                f'<div style="font-size:0.95em;font-weight:700;color:{color}">'
                f"{sign}{pct:.2f}%</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

    _row(f"Top {n} ▲", df.nlargest(n, "return_pct"))
    _row(f"Top {n} ▼", df.nsmallest(n, "return_pct"))


# ---------------------------------------------------------------------------
# Tab entry point
# ---------------------------------------------------------------------------


def render_heatmap_tab(
    df: pd.DataFrame,
    index_key: str = "all",
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    size_by: str = "dollar_volume",
) -> None:
    """Render the full Heatmap tab: sector filter, color controls, movers strip, view toggle, treemap or ranked table."""
    # ------------------------------------------------------------------
    # Controls row — sector filter + color settings (one compact line)
    # ------------------------------------------------------------------
    all_sectors = sorted(df["group_name"].dropna().unique().tolist())

    # Clamp any stale session values to sectors present in this universe
    if "heatmap_sector_filter" not in st.session_state:
        st.session_state["heatmap_sector_filter"] = []
    _stored = st.session_state["heatmap_sector_filter"]
    _clamped = [s for s in _stored if s in all_sectors]
    if _clamped != _stored:
        st.session_state["heatmap_sector_filter"] = _clamped

    if "heatmap_clip" not in st.session_state:
        st.session_state["heatmap_clip"] = 50

    with st.expander("Options", expanded=False):
        _view_col, _f_col, _pal_col = st.columns([2, 3, 2], gap="large")

        with _f_col:
            selected_sectors = st.multiselect(
                "Sectors",
                options=all_sectors,
                key="heatmap_sector_filter",
                placeholder="All sectors",
                label_visibility="collapsed",
            )
        color_scale = _PALETTE_OPTIONS[
            _pal_col.selectbox(
                "Color palette",
                list(_PALETTE_OPTIONS.keys()),
                key="color_palette",
                label_visibility="collapsed",
            )
        ]
        view_toggle = _view_col.radio(
            "View",
            ["Treemap", "Ranked Table"],
            key="heatmap_view_toggle",
            label_visibility="collapsed",
        )

        # --- Clip slider + Center-on-0 toggle (hidden — heatmap now colours
        #     by percentile rank so these have no effect; uncomment to restore
        #     absolute-return colouring with manual clip range) ---
        # clip = st.slider(
        #     "Clip ±%", min_value=1, max_value=100,
        #     key="heatmap_clip", format="±%d%%",
        # )
        # center_zero = st.toggle("Center on 0%", value=True, key="center_zero")

    # Percentile mode ignores these but keeps the call signature stable.
    color_range = (0.0, 100.0)
    center_zero = True

    if selected_sectors:
        df = df[df["group_name"].isin(selected_sectors)].copy()

    if df.empty:
        st.info(
            "No data for the selected sectors — clear the filter to see all symbols."
        )
        return

    if view_toggle == "Treemap":
        # Prepare values column based on size_by mode
        _values_col = "dollar_volume"
        _plot_df = df
        if size_by == "equal_weight":
            _plot_df = df.assign(_size=1.0)
            _values_col = "_size"
        elif size_by == "magnitude":
            _plot_df = df.assign(_size=df["return_pct"].abs().clip(lower=0.01))
            _values_col = "_size"

        _render_movers_strip(df, color_range, color_scale, center_zero)
        fig = build_fig(
            _plot_df,
            color_range=color_range,
            color_scale=color_scale,
            center_zero=center_zero,
            values_col=_values_col,
        )
        st.plotly_chart(fig, use_container_width=True, theme=None)
    else:
        render_ranked_table(
            df, color_range, color_scale=color_scale, center_zero=center_zero
        )

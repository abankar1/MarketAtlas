"""
Heatmap tab — treemap and ranked-table views.

render_heatmap_tab(df, color_range, range_label, index_key, date_from, date_to)
    renders the full tab; includes a CSV download button for the heatmap data.
render_ranked_table(df, color_range)
    ranked table helper (symbol, name, sector, return %, percentile, volume).
"""
from __future__ import annotations

import datetime as dt
import io

import pandas as pd
import plotly.colors as pc
import streamlit as st

from src.dashboard.charts import build_fig


# ---------------------------------------------------------------------------
# Ranked table
# ---------------------------------------------------------------------------

def render_ranked_table(df: pd.DataFrame, color_range: tuple[float, float]) -> None:
    """
    Sorted table of all symbols: rank, symbol, name, sector, return %,
    percentile, dollar volume, volume, start/end close.

    Return % cells are background-colored with the same RdYlGn palette and
    clip range as the treemap (no matplotlib required — uses Plotly colorscale).
    Percentile column shows a CSS bar from red (0) to green (100).
    """
    tdf = df[[
        "symbol", "name", "group_name",
        "return_pct", "dollar_volume", "end_volume",
        "start_close", "end_close",
    ]].copy()

    tdf["percentile_rank"] = (tdf["return_pct"].rank(ascending=True) / len(tdf) * 100)
    tdf = tdf.sort_values("return_pct", ascending=False).reset_index(drop=True)
    tdf.index = tdf.index + 1  # 1-based rank

    tdf = tdf.rename(columns={
        "symbol":          "Symbol",
        "name":            "Name",
        "group_name":      "Sector",
        "return_pct":      "Return %",
        "percentile_rank": "Percentile",
        "dollar_volume":   "Dollar Volume",
        "end_volume":      "Volume",
        "start_close":     "Start Close",
        "end_close":       "End Close",
    })

    vmin, vmax = color_range

    def _return_cell_style(val: float) -> str:
        span = vmax - vmin
        t = max(0.0, min(1.0, (val - vmin) / span)) if span else 0.5
        rgb = pc.sample_colorscale("RdYlGn", [t])[0]
        return f"background-color: {rgb}; color: black"

    styled = (
        tdf.style
        .map(_return_cell_style, subset=["Return %"])
        .bar(subset=["Percentile"], color=["#ef5350", "#26a69a"], vmin=0, vmax=100)
        .format({
            "Return %":     "{:+.2f}%",
            "Percentile":   "{:.0f}",
            "Dollar Volume": "${:,.0f}",
            "Volume":        "{:,.0f}",
            "Start Close":   "${:.2f}",
            "End Close":     "${:.2f}",
        })
    )

    st.caption(
        f"{len(tdf)} symbols · sortable by any column · "
        "color scale clipped to the same ±range as the treemap"
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
    target_year  = d_from.year + (target_month - 1) // 12
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
# Tab entry point
# ---------------------------------------------------------------------------

def render_heatmap_tab(
    df: pd.DataFrame,
    color_range: tuple[float, float],
    range_label: str,
    index_key: str = "all",
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> None:
    """Render the full Heatmap tab: KPI row, view toggle, treemap or ranked table."""
    # KPI row
    cols = st.columns(4)
    cols[0].metric("Symbols", f"{len(df)}")
    cols[1].metric(f"Median return ({range_label})", f"{df['return_pct'].median():.2f}%")
    cols[2].metric(
        f"Best ({range_label})",
        f"{df.loc[df['return_pct'].idxmax(), 'symbol']} ({df['return_pct'].max():.2f}%)",
    )
    cols[3].metric(
        f"Worst ({range_label})",
        f"{df.loc[df['return_pct'].idxmin(), 'symbol']} ({df['return_pct'].min():.2f}%)",
    )

    # View toggle + CSV download in the same row
    _toggle_col, _dl_col = st.columns([4, 1])
    with _toggle_col:
        view_toggle = st.radio(
            "View",
            ["Treemap", "Ranked Table"],
            horizontal=True,
            key="heatmap_view_toggle",
            label_visibility="collapsed",
        )
    with _dl_col:
        _d_from = date_from.isoformat() if date_from else "start"
        _d_to   = date_to.isoformat()   if date_to   else "end"
        _over_limit = (
            _exceeds_three_months(date_from, date_to)
            if (date_from and date_to) else False
        )
        st.download_button(
            label="⬇ Export CSV",
            data=_build_export_csv(df) if not _over_limit else b"",
            file_name=f"{index_key}_{_d_from}_{_d_to}_heatmap.csv",
            mime="text/csv",
            use_container_width=True,
            disabled=_over_limit,
            help=(
                "Export limited to ranges of 3 months or less — narrow the date range to enable."
                if _over_limit
                else "Download symbol, name, sector, return %, dollar volume, percentile rank"
            ),
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

"""
Heatmap tab — treemap and ranked-table views.

render_heatmap_tab(df, color_range, range_label)   renders the full tab
render_ranked_table(df, color_range)               ranked table helper
"""
from __future__ import annotations

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
# Tab entry point
# ---------------------------------------------------------------------------

def render_heatmap_tab(
    df: pd.DataFrame,
    color_range: tuple[float, float],
    range_label: str,
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

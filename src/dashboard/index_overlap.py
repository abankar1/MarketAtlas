"""
Index Overlap tab — cross-membership summary for S&P 500, NASDAQ-100, and Dow 30.

render_index_overlap_tab(db_url)   tab entry point

Layout
------
1. Headline metrics  — total unique symbols, count per index
2. Bucket grid       — 7 mutual-exclusion buckets (exclusive / pairs / all three)
3. Bar chart         — bucket sizes at a glance
4. Symbol tables     — expandable per-bucket detail
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.dashboard.data import fetch_index_overlap


# ---------------------------------------------------------------------------
# Bucket definitions — fixed order, label, colour
# ---------------------------------------------------------------------------

# Key: (in_sp500, in_nasdaq100, in_dow30)
_BUCKETS: list[tuple[tuple[bool, bool, bool], str, str]] = [
    ((True,  False, False), "S&P 500 only",           "#1565C0"),
    ((False, True,  False), "NASDAQ-100 only",         "#6A1B9A"),
    ((False, False, True ), "Dow 30 only",             "#00695C"),
    ((True,  True,  False), "S&P 500 + NASDAQ-100",   "#4527A0"),
    ((True,  False, True ), "S&P 500 + Dow 30",       "#00838F"),
    ((False, True,  True ), "NASDAQ-100 + Dow 30",    "#2E7D32"),
    ((True,  True,  True ), "All three indices",       "#E65100"),
]


# ---------------------------------------------------------------------------
# Tab entry point
# ---------------------------------------------------------------------------

def render_index_overlap_tab(db_url: str) -> None:
    """Render the full Index Overlap tab."""
    with st.spinner("Loading membership data..."):
        df = fetch_index_overlap(db_url)

    if df.empty:
        st.warning("No constituent data found. Run the constituent sync first.")
        return

    # ------------------------------------------------------------------ #
    # 1. Headline counts
    # ------------------------------------------------------------------ #
    n_sp500   = int(df["in_sp500"].sum())
    n_nasdaq  = int(df["in_nasdaq100"].sum())
    n_dow     = int(df["in_dow30"].sum())
    n_unique  = len(df)

    # 4-up marker keeps all 4 metrics on a single line at every width.
    st.markdown('<div data-kpi-row-4="true"></div>', unsafe_allow_html=True)
    h = st.columns(4)
    h[0].metric("Unique symbols", n_unique)
    h[1].metric("S&P 500",    n_sp500)
    h[2].metric("NASDAQ-100", n_nasdaq)
    h[3].metric("Dow 30",     n_dow)

    st.markdown("---")

    # ------------------------------------------------------------------ #
    # 2. Compute all seven buckets
    # ------------------------------------------------------------------ #
    buckets: list[dict] = []
    for (sp, nd, dw), label, color in _BUCKETS:
        mask    = (
            (df["in_sp500"]    == sp) &
            (df["in_nasdaq100"] == nd) &
            (df["in_dow30"]    == dw)
        )
        sub     = df.loc[mask, ["symbol", "name", "sector"]].reset_index(drop=True)
        buckets.append({"key": (sp, nd, dw), "label": label,
                        "color": color, "count": len(sub), "df": sub})

    # ------------------------------------------------------------------ #
    # 3. Horizontal bar chart — bucket sizes
    # ------------------------------------------------------------------ #
    st.subheader("Cross-index membership")
    non_empty = [b for b in buckets if b["count"] > 0]
    fig = go.Figure(go.Bar(
        x=[b["count"] for b in non_empty],
        y=[b["label"] for b in non_empty],
        orientation="h",
        marker_color=[b["color"] for b in non_empty],
        text=[str(b["count"]) for b in non_empty],
        textposition="auto",
        hovertemplate="<b>%{y}</b><br>Symbols: %{x}<extra></extra>",
    ))
    fig.update_layout(
        height=max(300, len(non_empty) * 52),
        margin=dict(t=20, l=10, r=20, b=20),
        xaxis_title="Symbol count",
        yaxis=dict(autorange="reversed", tickfont=dict(size=12), automargin=True),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(gridcolor="rgba(128,128,128,0.2)", zeroline=False, automargin=True),
    )
    st.plotly_chart(fig, use_container_width=True, theme=None)

    st.markdown("---")

    # ------------------------------------------------------------------ #
    # 5. Per-bucket symbol tables (expandable)
    # ------------------------------------------------------------------ #
    st.subheader("Symbols by bucket")
    for b in buckets:
        if b["count"] == 0:
            continue
        with st.expander(f"{b['label']} — {b['count']} symbols"):
            display = b["df"].copy()
            display.index = display.index + 1
            display.columns = ["Symbol", "Name", "Sector"]
            st.dataframe(display, use_container_width=True)

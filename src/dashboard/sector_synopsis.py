"""
Sector Synopsis tab — KPI row, ranked bar chart, inline stock detail.

render_sector_synopsis_tab(df, range_label, db_url, date_from, date_to)
    Tab entry point: renders the sector dropdown then delegates to the fragment.

render_sector_synopsis(df, sector, range_label, db_url, date_from, date_to)
    @st.fragment — bar chart + click-to-expand stock detail.
    Fragment scope means bar-click reruns stay within this panel and do not
    reset the active tab.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import plotly.colors as pc
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as st_components

from src.dashboard.charts import build_detail_fig
from src.dashboard.data import get_ohlcv_cached


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Maps session_state color_palette label → Plotly colorscale name
_PALETTE_MAP: dict[str, str] = {
    "RdYlGn (default)":       "RdYlGn",
    "RdBu (colorblind-safe)": "RdBu",
    "Viridis (sequential)":   "Viridis",
}


def _fmt_dollar(v: float) -> str:
    """Compact dollar formatter: $1.2B / $450.3M / $12.5K / $999."""
    a = abs(v)
    if a >= 1e9:
        return f"${v/1e9:.1f}B"
    if a >= 1e6:
        return f"${v/1e6:.1f}M"
    if a >= 1e3:
        return f"${v/1e3:.1f}K"
    return f"${v:,.0f}"


# ---------------------------------------------------------------------------
# Cross-sector breadth helpers
# ---------------------------------------------------------------------------


def _compute_sector_breadth(df: pd.DataFrame) -> pd.DataFrame:
    """
    Return a DataFrame with one row per sector:
      group_name, breadth_pct (0-100), n_up, n_total.
    Sorted ascending by breadth_pct (for horizontal bar — lowest at bottom).
    """
    grp = df.groupby("group_name")["return_pct"]
    result = pd.DataFrame({
        "breadth_pct": grp.apply(lambda s: (s > 0).mean() * 100),
        "n_up":        grp.apply(lambda s: int((s > 0).sum())),
        "n_total":     grp.count(),
    }).reset_index()
    return result.sort_values("breadth_pct", ascending=True).reset_index(drop=True)


def _render_breadth_bar(breadth: pd.DataFrame) -> None:
    """
    Horizontal bar chart: all sectors, sorted by breadth ascending
    (highest at top), coloured with the active heatmap palette.
    """
    _palette_label = st.session_state.get("color_palette", "RdYlGn (default)")
    _color_scale   = _PALETTE_MAP.get(_palette_label, "RdYlGn")

    bar_colors = [
        pc.sample_colorscale(_color_scale, [b / 100])[0]
        for b in breadth["breadth_pct"]
    ]
    hover_texts = [
        f"<b>{row['group_name']}</b><br>"
        f"Breadth: {row['breadth_pct']:.0f}%<br>"
        f"{int(row['n_up'])}/{int(row['n_total'])} stocks up"
        for _, row in breadth.iterrows()
    ]

    fig = go.Figure(go.Bar(
        x=breadth["breadth_pct"],
        y=breadth["group_name"],
        orientation="h",
        marker_color=bar_colors,
        text=[f"{b:.0f}%" for b in breadth["breadth_pct"]],
        textposition="outside",
        hovertext=hover_texts,
        hoverinfo="text",
    ))
    fig.add_vline(
        x=50,
        line_color="rgba(255,255,255,0.3)",
        line_width=1,
        line_dash="dot",
        annotation_text="50%",
        annotation_position="top",
        annotation_font_color="rgba(255,255,255,0.4)",
    )
    fig.update_layout(
        height=max(220, len(breadth) * 28),
        margin=dict(t=10, l=10, r=60, b=30),
        xaxis=dict(
            range=[0, 115],
            title="% of stocks up",
            gridcolor="rgba(255,255,255,0.1)",
            zeroline=False,
        ),
        yaxis=dict(tickfont=dict(size=11)),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font_color="white",
    )

    st.subheader("Sector Breadth")
    st.caption(
        f"% of stocks with a positive return · {len(breadth)} sectors · "
        "dotted line = 50% · click a bar to drill into that sector"
    )
    return st.plotly_chart(
        fig,
        use_container_width=True,
        theme=None,
        on_select="rerun",
        key="breadth_bar",
    )


# ---------------------------------------------------------------------------
# Fragment — bar chart + inline detail
# ---------------------------------------------------------------------------

@st.fragment
def render_sector_synopsis(
    df,
    sector: str,
    range_label: str,
    db_url: str,
    date_from: dt.date,
    date_to: dt.date,
) -> None:
    """
    Render KPIs + ranked bar chart for a single GICS sector.

    Decorated @st.fragment so that clicking a bar (on_select="rerun") triggers
    only a fragment rerun — the active tab and sector dropdown are unaffected.
    """
    sdf = df[df["group_name"] == sector].copy()

    if sdf.empty:
        st.warning(f"No data for sector: {sector}")
        return

    sdf = sdf.sort_values("return_pct", ascending=False).reset_index(drop=True)

    median_ret  = sdf["return_pct"].median()
    avg_ret     = sdf["return_pct"].mean()
    gainers     = int((sdf["return_pct"] > 0).sum())
    breadth_pct = gainers / len(sdf) * 100
    total_dvol  = sdf["dollar_volume"].sum()
    best        = sdf.iloc[0]
    worst       = sdf.iloc[-1]

    # KPI row
    # range_label shown once as caption; removed from individual metric headings
    # to prevent truncation. Column widths weighted by content needs.
    st.caption(f"Period: {range_label}")
    k = st.columns([0.7, 1.6, 1.6, 1.8, 1.7])
    k[0].metric("Stocks", len(sdf))
    k[1].metric("Median return", f"{median_ret:+.2f}%")
    k[2].metric("Avg return",    f"{avg_ret:+.2f}%")
    k[3].metric("Breadth",       f"{breadth_pct:.0f}% ({gainers}/{len(sdf)} up)")
    k[4].metric("Total dollar volume", _fmt_dollar(total_dvol))

    st.markdown("---")

    # Auto-generated text summary
    direction = "outperformed" if avg_ret > 0 else "underperformed"
    st.markdown(
        f"**{sector}** had **{len(sdf)} stocks** in the selected period. "
        f"The sector {direction} with an average return of **{avg_ret:+.2f}%** "
        f"(median: **{median_ret:+.2f}%**). "
        f"Breadth was **{breadth_pct:.0f}%** ({gainers}/{len(sdf)} stocks up). "
        f"Best performer: **{best['symbol']}** ({best['return_pct']:+.2f}%). "
        f"Worst performer: **{worst['symbol']}** ({worst['return_pct']:+.2f}%)."
    )

    st.markdown("---")

    # Horizontal bar chart — stocks ranked by return %
    bar_colors = ["#26a69a" if r >= 0 else "#ef5350" for r in sdf["return_pct"]]

    fig = go.Figure(go.Bar(
        x=sdf["return_pct"],
        y=sdf["symbol"],
        orientation="h",
        marker_color=bar_colors,
        text=[f"{r:+.2f}%" for r in sdf["return_pct"]],
        textposition="outside",
        customdata=sdf.assign(
            _ret=sdf["return_pct"].map(lambda v: f"{v:+.2f}%")
        )[["name", "start_close", "end_close", "dollar_volume", "_ret"]].values,
        hovertemplate=(
            "<b>%{y}</b>  %{customdata[0]}<br>"
            "Return: <b>%{customdata[4]}</b><br>"
            "Start: $%{customdata[1]:.2f} → End: $%{customdata[2]:.2f}<br>"
            "Dollar volume: $%{customdata[3]:,.0f}"
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
        xaxis=dict(
            gridcolor="rgba(255,255,255,0.1)",
            zeroline=False,
            tickformat="+.2f",
            hoverformat="+.2f",
            ticksuffix="%",
        ),
    )

    event = st.plotly_chart(
        fig,
        use_container_width=True,
        theme=None,
        on_select="rerun",
        key=f"sector_bar_{sector}",
    )

    # Inline stock detail on bar click
    clicked_points = (event.selection or {}).get("points", [])
    if clicked_points:
        clicked_symbol = clicked_points[0].get("y")
        if clicked_symbol:
            st.markdown('<div id="sector-stock-detail"></div>', unsafe_allow_html=True)
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



# ---------------------------------------------------------------------------
# Tab entry point
# ---------------------------------------------------------------------------

def render_sector_synopsis_tab(
    df,
    range_label: str,
    db_url: str,
    date_from: dt.date,
    date_to: dt.date,
) -> None:
    """
    Render the full Sector Synopsis tab: sector dropdown + synopsis fragment.

    The selectbox lives outside the fragment (changing the sector triggers a
    full page rerun by design — it fetches a new slice of df).  The bar chart
    and inline detail inside render_sector_synopsis are fragment-scoped so that
    clicking a bar does not rerun the whole page.
    """
    sectors = sorted(df["group_name"].dropna().unique())
    if not sectors:
        st.warning("No sector data available for this universe.")
        return

    # Cross-sector overview — all sectors sorted by breadth
    # on_select="rerun" fires when user clicks a bar; we latch the value into
    # synopsis_sector so the dropdown below reflects the click.
    # _breadth_applied tracks the last value we pushed from the chart so that
    # normal page reruns (e.g. date change) don't re-override a manual dropdown
    # selection — the latch is cleared whenever the dropdown diverges from it.
    _breadth_event = _render_breadth_bar(_compute_sector_breadth(df))
    _clicked_pts   = (_breadth_event.selection or {}).get("points", [])

    if _clicked_pts:
        _clicked_sector = _clicked_pts[0].get("y")
        if (_clicked_sector
                and _clicked_sector in sectors
                and _clicked_sector != st.session_state.get("_breadth_applied")):
            st.session_state["_breadth_applied"] = _clicked_sector
            st.session_state["synopsis_sector"]  = _clicked_sector
    else:
        # Bar was deselected — release the latch so re-clicking the same bar works
        st.session_state.pop("_breadth_applied", None)

    st.divider()

    selected_sector = st.selectbox("Select sector", sectors, key="synopsis_sector")

    # If the user overrode the breadth selection via the dropdown, clear the latch
    # so a future click on the same bar can re-apply it.
    if (st.session_state.get("_breadth_applied")
            and selected_sector != st.session_state.get("_breadth_applied")):
        st.session_state.pop("_breadth_applied", None)

    render_sector_synopsis(df, selected_sector, range_label, db_url, date_from, date_to)

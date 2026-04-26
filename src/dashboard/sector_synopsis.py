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

import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as st_components

from src.dashboard.charts import build_detail_fig
from src.dashboard.data import get_ohlcv_cached


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
    gainers     = (sdf["return_pct"] > 0).sum()
    losers      = (sdf["return_pct"] < 0).sum()
    total_dvol  = sdf["dollar_volume"].sum()
    best        = sdf.iloc[0]
    worst       = sdf.iloc[-1]

    # KPI row
    k = st.columns(5)
    k[0].metric("Stocks", len(sdf))
    k[1].metric(f"Median return ({range_label})", f"{median_ret:+.2f}%")
    k[2].metric(f"Avg return ({range_label})", f"{avg_ret:+.2f}%")
    k[3].metric("Gainers / Losers", f"{gainers} / {losers}")
    k[4].metric("Total dollar volume", f"${total_dvol:,.0f}")

    st.markdown("---")

    # Auto-generated text summary
    direction = "outperformed" if avg_ret > 0 else "underperformed"
    st.markdown(
        f"**{sector}** had **{len(sdf)} stocks** in the selected period. "
        f"The sector {direction} with an average return of **{avg_ret:+.2f}%** "
        f"(median: **{median_ret:+.2f}%**). "
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
        hovertemplate="<b>%{y}</b><br>Return: %{x:.2f}%<br><extra></extra>",
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
        xaxis=dict(gridcolor="rgba(255,255,255,0.1)", zeroline=False),
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

    with st.expander("Show raw data"):
        display = sdf[
            ["symbol", "name", "return_pct", "start_close", "end_close", "dollar_volume"]
        ].copy()
        display.columns = ["Symbol", "Name", "Return %", "Start Close", "End Close", "Dollar Volume"]
        display["Return %"]      = display["Return %"].map(lambda x: f"{x:+.2f}%")
        display["Start Close"]   = display["Start Close"].map(lambda x: f"${x:.2f}")
        display["End Close"]     = display["End Close"].map(lambda x: f"${x:.2f}")
        display["Dollar Volume"] = display["Dollar Volume"].map(lambda x: f"${x:,.0f}")
        st.dataframe(display, use_container_width=True, hide_index=True)


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

    selected_sector = st.selectbox("Select sector", sectors, key="synopsis_sector")
    render_sector_synopsis(df, selected_sector, range_label, db_url, date_from, date_to)

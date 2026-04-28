"""
Plotly chart builders shared across dashboard tabs.

build_fig(df, color_range)              Treemap heatmap figure
build_detail_fig(df, symbol, active)    3-panel candlestick figure
build_compare_fig(series, primary)      Normalised multi-symbol comparison
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src.dashboard.indicators import (
    compute_atr,
    compute_bollinger_bands,
    compute_ema,
    compute_macd,
    compute_obv,
    compute_rsi,
    compute_sma,
)


# ---------------------------------------------------------------------------
# Heatmap treemap
# ---------------------------------------------------------------------------

def build_fig(
    df: pd.DataFrame,
    color_range: tuple[float, float],  # retained for API compat; ignored
    color_scale: str = "RdYlGn",
    center_zero: bool = True,           # retained for API compat; ignored
    values_col: str = "dollar_volume",
) -> go.Figure:
    """
    Build the treemap colored by percentile rank of return % (0 = worst,
    100 = best within the current universe).

    Percentile coloring keeps the scale well-distributed regardless of
    outliers — a single +354% return no longer washes everything else
    into the same boundary green.

    Implemented with go.Treemap so we control sector parent nodes
    explicitly: each sector parent gets its own dollar-volume-weighted
    return %, percentile (mean of children), and pre-formatted hover
    string. Hover precision is therefore exact at every level.

    values_col     Column used to size tiles ('dollar_volume', '_size' for
                   equal-weight or magnitude modes).
    color_scale    Any Plotly named colorscale ('RdYlGn', 'RdBu', 'Viridis', …)
    """
    df = df.copy()
    if len(df) > 1:
        df["percentile"] = df["return_pct"].rank(method="average", pct=True) * 100
    else:
        df["percentile"] = 50.0

    # ---- Sector aggregates ------------------------------------------------
    def _weighted_return(g: pd.DataFrame) -> float:
        w = g["dollar_volume"].sum()
        return float((g["return_pct"] * g["dollar_volume"]).sum() / w) if w else float(g["return_pct"].mean())

    def _weighted_close(g: pd.DataFrame, col: str) -> float:
        w = g["dollar_volume"].sum()
        return float((g[col] * g["dollar_volume"]).sum() / w) if w else float(g[col].mean())

    sector_agg = (
        df.groupby("group_name", dropna=False)
        .apply(
            lambda g: pd.Series(
                {
                    "value":          float(g[values_col].sum()),
                    "return_pct":     _weighted_return(g),
                    "percentile":     float(g["percentile"].mean()),
                    "dollar_volume":  float(g["dollar_volume"].sum()),
                    "n_stocks":       int(len(g)),
                    "start_close":    _weighted_close(g, "start_close"),
                    "end_close":      _weighted_close(g, "end_close"),
                }
            ),
            include_groups=False,
        )
        .reset_index()
    )

    # ---- Build flat node lists (sectors first, then stocks) ---------------
    leaf_ids   = (df["group_name"].astype(str) + "/" + df["symbol"].astype(str)).tolist()
    sector_ids = sector_agg["group_name"].astype(str).tolist()

    ids       = sector_ids + leaf_ids
    labels    = sector_agg["group_name"].astype(str).tolist() + df["symbol"].astype(str).tolist()
    parents   = [""] * len(sector_agg) + df["group_name"].astype(str).tolist()
    values    = sector_agg["value"].tolist() + df[values_col].astype(float).tolist()
    colors    = sector_agg["percentile"].tolist() + df["percentile"].astype(float).tolist()

    # customdata layout: [return_str, close1_line, close2_line, dollar_volume_line]
    sector_custom = [
        [
            f"{r['return_pct']:+.2f}%",                              # weighted-avg return
            f"Avg start close: ${r['start_close']:.2f}",             # dollar-volume-weighted
            f"Avg end close: ${r['end_close']:.2f}",                 # dollar-volume-weighted
            f"${r['dollar_volume']:,.0f} · {int(r['n_stocks'])} stocks",
        ]
        for _, r in sector_agg.iterrows()
    ]
    leaf_custom = [
        [
            f"{row.return_pct:+.2f}%",
            f"Start close: ${row.start_close:.2f}",
            f"End close: ${row.end_close:.2f}",
            f"${row.dollar_volume:,.0f}",
        ]
        for row in df.itertuples()
    ]
    customdata = sector_custom + leaf_custom

    _SEQUENTIAL = {"Viridis", "Plasma", "Inferno", "Magma", "Cividis"}
    is_sequential = isinstance(color_scale, str) and color_scale in _SEQUENTIAL
    midpoint = None if is_sequential else 50.0

    fig = go.Figure(
        go.Treemap(
            ids=ids,
            labels=labels,
            parents=parents,
            values=values,
            branchvalues="total",
            customdata=customdata,
            marker=dict(
                colors=colors,
                colorscale=color_scale,
                cmin=0.0,
                cmax=100.0,
                cmid=midpoint,
                line=dict(width=0),
                colorbar=dict(title="Percentile", len=0.85),
            ),
            hovertemplate=(
                "<b>%{label}</b><br>"
                "Return: <b>%{customdata[0]}</b><br>"
                "Percentile: %{color:.0f}<br>"
                "%{customdata[1]}<br>"
                "%{customdata[2]}<br>"
                "Dollar volume: %{customdata[3]}<br>"
                "<extra></extra>"
            ),
        )
    )

    fig.update_layout(
        height=640,
        margin=dict(t=20, l=6, r=6, b=6),
        hoverlabel=dict(bgcolor="white", font_size=13, font_color="black"),
    )

    return fig


# ---------------------------------------------------------------------------
# Per-stock candlestick detail
# ---------------------------------------------------------------------------

def build_detail_fig(
    df: pd.DataFrame, symbol: str, active: list[str]
) -> go.Figure:
    """
    Build a multi-panel figure for a single stock.

    Fixed panels
    ------------
    Row 1  Candlestick + price overlays (SMA 20/50, EMA 20, Bollinger Bands)
    Row 2  Volume bars (green/red)

    Optional sub-panels (one row each, rendered in this order if selected)
    -----------------------------------------------------------------------
    RSI    14-period Relative Strength Index (overbought 70 / oversold 30)
    MACD   12/26/9 — line + signal + histogram bars
    ATR    14-period Average True Range (volatility proxy)
    OBV    On-Balance Volume (volume-weighted trend)

    Row count and heights are computed dynamically from `active`.
    """
    close  = df["close"]
    dates  = df["date"]

    # Sub-panels rendered in a fixed order regardless of multiselect order
    _SUB_PANEL_ORDER = ["RSI", "MACD", "ATR", "OBV"]
    sub_panels = [p for p in _SUB_PANEL_ORDER if p in active]
    n_sub      = len(sub_panels)
    n_rows     = 2 + n_sub

    # Row heights: price dominates; each additional panel shrinks it slightly
    if n_sub == 0:
        row_heights = [0.72, 0.28]
        fig_height  = 500
    else:
        price_h  = max(0.45, 0.60 - 0.03 * n_sub)
        vol_h    = 0.12 if n_sub >= 3 else 0.15
        panel_h  = round((1.0 - price_h - vol_h) / n_sub, 4)
        row_heights = [price_h, vol_h] + [panel_h] * n_sub
        fig_height  = 580 + 160 * n_sub

    _panel_titles = {
        "RSI":  "RSI (14)",
        "MACD": "MACD (12/26/9)",
        "ATR":  "ATR (14)",
        "OBV":  "OBV",
    }
    subplot_titles = ["", "Volume"] + [_panel_titles[p] for p in sub_panels]

    # Map indicator name → row number (1-based)
    panel_row = {p: 3 + i for i, p in enumerate(sub_panels)}

    fig = make_subplots(
        rows=n_rows,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=row_heights,
        subplot_titles=subplot_titles,
    )

    # ------------------------------------------------------------------
    # Row 1 — Candlestick
    # ------------------------------------------------------------------
    fig.add_trace(
        go.Candlestick(
            x=dates,
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=close,
            name=symbol,
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
        ),
        row=1, col=1,
    )

    if "SMA 20" in active:
        fig.add_trace(
            go.Scatter(x=dates, y=compute_sma(close, 20), name="SMA 20",
                       line=dict(color="#1976D2", width=1.2)),
            row=1, col=1,
        )
    if "SMA 50" in active:
        fig.add_trace(
            go.Scatter(x=dates, y=compute_sma(close, 50), name="SMA 50",
                       line=dict(color="#F57C00", width=1.2)),
            row=1, col=1,
        )
    if "EMA 20" in active:
        fig.add_trace(
            go.Scatter(x=dates, y=compute_ema(close, 20), name="EMA 20",
                       line=dict(color="#7B1FA2", width=1.2, dash="dot")),
            row=1, col=1,
        )
    if "Bollinger Bands" in active:
        bb = compute_bollinger_bands(close)
        fig.add_trace(
            go.Scatter(x=dates, y=bb["bb_upper"], name="BB Upper",
                       line=dict(color="#78909C", width=1, dash="dash"), showlegend=True),
            row=1, col=1,
        )
        fig.add_trace(
            go.Scatter(x=dates, y=bb["bb_mid"], name="BB Mid",
                       line=dict(color="#78909C", width=0.8, dash="dot"), showlegend=False),
            row=1, col=1,
        )
        fig.add_trace(
            go.Scatter(x=dates, y=bb["bb_lower"], name="BB Lower",
                       line=dict(color="#78909C", width=1, dash="dash"),
                       fill="tonexty", fillcolor="rgba(120,144,156,0.08)", showlegend=False),
            row=1, col=1,
        )

    # ------------------------------------------------------------------
    # Row 2 — Volume bars (coloured by candle direction)
    # ------------------------------------------------------------------
    bar_colors = np.where(df["close"].values >= df["open"].values, "#26a69a", "#ef5350")
    fig.add_trace(
        go.Bar(x=dates, y=df["volume"], name="Volume",
               marker_color=bar_colors, showlegend=False),
        row=2, col=1,
    )

    # ------------------------------------------------------------------
    # Optional sub-panels
    # ------------------------------------------------------------------
    if "RSI" in active:
        r = panel_row["RSI"]
        rsi = compute_rsi(close)
        fig.add_trace(
            go.Scatter(x=dates, y=rsi, name="RSI 14",
                       line=dict(color="#FF6F00", width=1.5), showlegend=False),
            row=r, col=1,
        )
        fig.add_hline(y=70, line_dash="dot", line_color="red",   row=r, col=1)
        fig.add_hline(y=30, line_dash="dot", line_color="green", row=r, col=1)
        fig.update_yaxes(range=[0, 100], row=r, col=1)

    if "MACD" in active:
        r    = panel_row["MACD"]
        macd = compute_macd(close)
        # Histogram bars: green above zero, red below
        hist_colors = np.where(macd["histogram"].values >= 0, "#26a69a", "#ef5350")
        fig.add_trace(
            go.Bar(x=dates, y=macd["histogram"], name="MACD Hist",
                   marker_color=hist_colors, opacity=0.6, showlegend=False),
            row=r, col=1,
        )
        fig.add_trace(
            go.Scatter(x=dates, y=macd["macd"], name="MACD",
                       line=dict(color="#1976D2", width=1.4), showlegend=False),
            row=r, col=1,
        )
        fig.add_trace(
            go.Scatter(x=dates, y=macd["signal"], name="Signal",
                       line=dict(color="#FF6F00", width=1.2, dash="dot"), showlegend=False),
            row=r, col=1,
        )
        fig.add_hline(y=0, line_color="rgba(255,255,255,0.2)", line_width=1,
                      row=r, col=1)

    if "ATR" in active:
        r   = panel_row["ATR"]
        atr = compute_atr(df["high"], df["low"], close)
        fig.add_trace(
            go.Scatter(x=dates, y=atr, name="ATR 14",
                       line=dict(color="#AB47BC", width=1.4), showlegend=False,
                       fill="tozeroy", fillcolor="rgba(171,71,188,0.08)"),
            row=r, col=1,
        )

    if "OBV" in active:
        r   = panel_row["OBV"]
        obv = compute_obv(close, df["volume"])
        fig.add_trace(
            go.Scatter(x=dates, y=obv, name="OBV",
                       line=dict(color="#26C6DA", width=1.4), showlegend=False),
            row=r, col=1,
        )

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    fig.update_layout(
        height=fig_height,
        margin=dict(t=30, l=60, r=20, b=20),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="Price",  row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    for p in sub_panels:
        fig.update_yaxes(title_text=_panel_titles[p], row=panel_row[p], col=1)

    return fig


# ---------------------------------------------------------------------------
# Normalised comparison chart
# ---------------------------------------------------------------------------

# Primary symbol gets a distinct bright colour; comparisons cycle through rest.
_COMPARE_COLORS = ["#FFD600", "#26C6DA", "#FF6F00", "#AB47BC"]


def build_compare_fig(
    series: dict[str, pd.DataFrame],
    primary: str,
) -> go.Figure:
    """
    Normalised performance comparison: each close series rebased to 100 at its
    first available bar so all symbols start at the same point.

    series   ordered dict {symbol: ohlcv_df}; primary should be first entry.
    primary  ticker of the main symbol (rendered wider + yellow).
    """
    fig = go.Figure()

    for i, (sym, df) in enumerate(series.items()):
        if df.empty:
            continue
        close      = df["close"]
        base       = close.iloc[0]
        normalized = close / base * 100

        is_primary = sym == primary
        color      = _COMPARE_COLORS[i % len(_COMPARE_COLORS)]
        width      = 2.5 if is_primary else 1.8

        # Pre-format return for hover (Plotly d3 arithmetic not reliable)
        ret_strs = [(f"{v - 100:+.2f}%") for v in normalized]

        fig.add_trace(go.Scatter(
            x=df["date"],
            y=normalized.round(2),
            mode="lines",
            name=sym,
            line=dict(color=color, width=width),
            customdata=ret_strs,
            hovertemplate=(
                f"<b>{sym}</b>: %{{y:.2f}}"
                "  (%{customdata})<extra></extra>"
            ),
        ))

    fig.add_hline(
        y=100,
        line_color="rgba(255,255,255,0.25)",
        line_width=1,
        line_dash="dot",
    )

    fig.update_layout(
        height=480,
        margin=dict(t=30, l=60, r=20, b=20),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        yaxis=dict(
            title="Normalised (base = 100)",
            gridcolor="rgba(255,255,255,0.1)",
            zeroline=False,
        ),
        xaxis=dict(gridcolor="rgba(255,255,255,0.1)", zeroline=False),
        xaxis_rangeslider_visible=False,
    )

    return fig

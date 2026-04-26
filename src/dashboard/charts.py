"""
Plotly chart builders shared across dashboard tabs.

build_fig(df, color_range)              Treemap heatmap figure
build_detail_fig(df, symbol, active)    3-panel candlestick figure
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
    color_range: tuple[float, float],
    color_scale: str = "RdYlGn",
    center_zero: bool = True,
    values_col: str = "dollar_volume",
) -> px.treemap:
    """
    Build the treemap colored by return %.

    values_col     Column used to size tiles ('dollar_volume', '_size' for
                   equal-weight or magnitude modes).
    color_scale    Any Plotly named colorscale ('RdYlGn', 'RdBu', 'Viridis', …)
    center_zero    True → diverging midpoint pinned at 0%.
                   False → midpoint at period median return.
                   Ignored for sequential scales (Viridis).
    """
    # Diverging scales benefit from an explicit midpoint; sequential ones don't.
    _SEQUENTIAL = {"Viridis", "Plasma", "Inferno", "Magma", "Cividis"}
    midpoint_kwargs: dict = {}
    if color_scale not in _SEQUENTIAL:
        midpoint_kwargs["color_continuous_midpoint"] = (
            0.0 if center_zero else float(df["return_pct"].median())
        )

    fig = px.treemap(
        df,
        path=["group_name", "symbol"],
        values=values_col,
        color="return_pct",
        hover_data={
            "start_close": ":.2f",
            "end_close": ":.2f",
            "dollar_volume": ":,.0f",
            "index_name": True,
        },
        color_continuous_scale=color_scale,
        range_color=color_range,
        **midpoint_kwargs,
    )

    fig.update_traces(
        marker=dict(line=dict(width=0)),
        hovertemplate=(
            "<b>%{label}</b><br>"
            "Return: %{color:.2f}%<br>"
            "Start close: %{customdata[0]:.2f}<br>"
            "End close: %{customdata[1]:.2f}<br>"
            "Dollar volume: %{customdata[2]:,.0f}<br>"
            "Index: %{customdata[3]}<br>"
            "<extra></extra>"
        ),
    )

    fig.update_layout(
        height=640,
        margin=dict(t=20, l=6, r=6, b=6),
        coloraxis_colorbar=dict(title="Return %", len=0.85),
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

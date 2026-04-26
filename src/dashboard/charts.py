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
    compute_bollinger_bands,
    compute_ema,
    compute_rsi,
    compute_sma,
)


# ---------------------------------------------------------------------------
# Heatmap treemap
# ---------------------------------------------------------------------------

def build_fig(df: pd.DataFrame, color_range: tuple[float, float]) -> px.treemap:
    """Build the RdYlGn treemap sized by dollar volume, colored by return %."""
    fig = px.treemap(
        df,
        path=["group_name", "symbol"],
        values="dollar_volume",
        color="return_pct",
        hover_data={
            "start_close": ":.2f",
            "end_close": ":.2f",
            "dollar_volume": ":,.0f",
            "index_name": True,
        },
        color_continuous_scale="RdYlGn",
        range_color=color_range,
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
    Build a 3-panel figure for a single stock:
      Row 1 — Candlestick + optional indicator overlays
      Row 2 — Volume bars (green/red)
      Row 3 — RSI (14)
    """
    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.60, 0.20, 0.20],
        subplot_titles=("", "Volume", "RSI (14)"),
    )

    close = df["close"]
    dates = df["date"]

    # Row 1 — Candlestick
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

    # Row 2 — Volume bars coloured by up/down candle
    bar_colors = np.where(df["close"].values >= df["open"].values, "#26a69a", "#ef5350")
    fig.add_trace(
        go.Bar(x=dates, y=df["volume"], name="Volume",
               marker_color=bar_colors, showlegend=False),
        row=2, col=1,
    )

    # Row 3 — RSI
    if "RSI" in active:
        rsi = compute_rsi(close)
        fig.add_trace(
            go.Scatter(x=dates, y=rsi, name="RSI 14",
                       line=dict(color="#FF6F00", width=1.5), showlegend=False),
            row=3, col=1,
        )
        fig.add_hline(y=70, line_dash="dot", line_color="red", row=3, col=1)
        fig.add_hline(y=30, line_dash="dot", line_color="green", row=3, col=1)

    fig.update_layout(
        height=800,
        margin=dict(t=30, l=60, r=20, b=20),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    fig.update_yaxes(title_text="RSI", row=3, col=1, range=[0, 100])

    return fig

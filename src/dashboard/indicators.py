"""
Technical indicator computations — pure pandas, no Streamlit or DB dependencies.

All functions accept pd.Series inputs aligned to the same index and return a
pd.Series or pd.DataFrame with the same index.

Available indicators
--------------------
compute_sma(close, window)                   Simple Moving Average
compute_ema(close, window)                   Exponential Moving Average
compute_bollinger_bands(close, window, std)  Upper / mid / lower bands
compute_rsi(close, window)                   Relative Strength Index (0–100)
compute_macd(close, fast, slow, signal)      MACD line + signal + histogram
compute_atr(high, low, close, window)        Average True Range
compute_obv(close, volume)                   On-Balance Volume

Minimum bar requirements (for meaningful values)
-------------------------------------------------
SMA 20          20 bars
SMA 50          50 bars
EMA 20          20 bars (converges faster than SMA)
Bollinger Bands 21 bars (20-period + 1 for rolling std)
RSI             15 bars (14-period Wilder smoothing)
MACD            35 bars (26 slow EMA + 9 signal EMA)
ATR             15 bars (14-period + 1 for prev-close true range)
OBV              2 bars (needs close.diff())

Usage:
    from src.dashboard.indicators import compute_macd, compute_atr, compute_obv

    macd = compute_macd(df["close"])          # DataFrame: macd, signal, histogram
    atr  = compute_atr(df["high"], df["low"], df["close"])
    obv  = compute_obv(df["close"], df["volume"])
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Trend overlays (plotted on the price panel)
# ---------------------------------------------------------------------------

def compute_sma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window).mean()


def compute_ema(close: pd.Series, window: int) -> pd.Series:
    return close.ewm(span=window, adjust=False).mean()


def compute_bollinger_bands(
    close: pd.Series, window: int = 20, num_std: float = 2.0
) -> pd.DataFrame:
    mid = close.rolling(window).mean()
    std = close.rolling(window).std()
    return pd.DataFrame(
        {
            "bb_upper": mid + num_std * std,
            "bb_mid":   mid,
            "bb_lower": mid - num_std * std,
        },
        index=close.index,
    )


# ---------------------------------------------------------------------------
# Sub-panel indicators
# ---------------------------------------------------------------------------

def compute_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    """Wilder-smoothed RSI (0–100)."""
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(window).mean()
    loss  = (-delta.clip(upper=0)).rolling(window).mean()
    rs    = gain / loss
    return (100 - (100 / (1 + rs))).rename("RSI")


def compute_macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """
    MACD = EMA(fast) − EMA(slow)
    Signal = EMA(MACD, signal)
    Histogram = MACD − Signal

    Returns a DataFrame with columns: macd, signal, histogram.
    Meaningful after ~35 bars (26 slow + 9 signal).
    """
    ema_fast    = close.ewm(span=fast,   adjust=False).mean()
    ema_slow    = close.ewm(span=slow,   adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram   = macd_line - signal_line
    return pd.DataFrame(
        {"macd": macd_line, "signal": signal_line, "histogram": histogram},
        index=close.index,
    )


def compute_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    window: int = 14,
) -> pd.Series:
    """
    Average True Range — Wilder exponential smoothing of the true range.

    True Range = max(high−low, |high−prev_close|, |low−prev_close|)
    Meaningful after ~15 bars.
    """
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=window, adjust=False).mean().rename("ATR")


def compute_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """
    On-Balance Volume — cumulative sum of signed volume.

    +volume on up days, −volume on down days, 0 on flat days.
    Meaningful after 2 bars.
    """
    direction = np.sign(close.diff()).fillna(0)
    return (direction * volume).cumsum().rename("OBV")

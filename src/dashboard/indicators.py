"""
Technical indicator computations — pure pandas, no Streamlit or DB dependencies.

All functions accept a pd.Series of closing prices and return a pd.Series
or pd.DataFrame aligned to the same index.

Available indicators:
    compute_sma(close, window)                  Simple Moving Average
    compute_ema(close, window)                  Exponential Moving Average
    compute_bollinger_bands(close, window, std)  Upper / mid / lower bands
    compute_rsi(close, window)                  Relative Strength Index (0–100)

Usage:
    from src.dashboard.indicators import compute_sma, compute_rsi

    sma20 = compute_sma(df["close"], window=20)
    rsi14 = compute_rsi(df["close"], window=14)
"""
from __future__ import annotations

import pandas as pd


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
            "bb_mid": mid,
            "bb_lower": mid - num_std * std,
        },
        index=close.index,
    )


def compute_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = (-delta.clip(upper=0)).rolling(window).mean()
    rs = gain / loss
    return (100 - (100 / (1 + rs))).rename("RSI")

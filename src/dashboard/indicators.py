"""
Pure pandas technical indicator computations.
No Streamlit, no DB dependencies — safe to import anywhere.
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

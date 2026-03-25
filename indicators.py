"""
indicators.py — Pure stateless indicator functions.
No IBKR code. No side effects. Fully unit-testable.

All functions accept a pandas DataFrame and return a new column or Series.
Input DataFrame must have columns: open, high, low, close, volume
"""

import pandas as pd
import numpy as np
from typing import Tuple


# ── EMA ──────────────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def add_emas(df: pd.DataFrame, fast: int = 9, slow: int = 21) -> pd.DataFrame:
    """Add EMA fast and slow columns to df. Returns new df."""
    df = df.copy()
    df[f"ema{fast}"] = ema(df["close"], fast)
    df[f"ema{slow}"] = ema(df["close"], slow)
    return df


# ── RSI ──────────────────────────────────────────────────────────────────────

def rsi(series: pd.Series, period: int = 7) -> pd.Series:
    """
    Wilder's RSI using EWMA (matches most charting platforms).
    Returns values 0–100.
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def add_rsi(df: pd.DataFrame, period: int = 7) -> pd.DataFrame:
    """Add RSI column. Returns new df."""
    df = df.copy()
    df["rsi"] = rsi(df["close"], period)
    return df


# ── VWAP ─────────────────────────────────────────────────────────────────────

def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """
    Intraday VWAP — resets at 09:30 each trading day.
    Requires a DatetimeIndex or 'timestamp' column.

    Formula: VWAP = cumsum(TP * Volume) / cumsum(Volume)
    where TP = (High + Low + Close) / 3
    """
    df = df.copy()

    if "timestamp" in df.columns:
        dates = pd.to_datetime(df["timestamp"]).dt.date
    elif isinstance(df.index, pd.DatetimeIndex):
        dates = df.index.date
    else:
        raise ValueError("DataFrame must have 'timestamp' column or DatetimeIndex")

    df["_tp"] = (df["high"] + df["low"] + df["close"]) / 3
    df["_tpv"] = df["_tp"] * df["volume"]
    df["_date"] = dates

    # Cumulative sums that reset each new day
    df["_cum_tpv"] = df.groupby("_date")["_tpv"].cumsum()
    df["_cum_vol"] = df.groupby("_date")["volume"].cumsum()

    df["vwap"] = df["_cum_tpv"] / df["_cum_vol"].replace(0, np.nan)

    # Clean up temp columns
    df.drop(columns=["_tp", "_tpv", "_date", "_cum_tpv", "_cum_vol"], inplace=True)
    return df


# ── ATR ──────────────────────────────────────────────────────────────────────

def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Average True Range for volatility-based position sizing."""
    df = df.copy()
    high_low = df["high"] - df["low"]
    high_prev_close = (df["high"] - df["close"].shift(1)).abs()
    low_prev_close = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=period, adjust=False).mean()
    return df


# ── All indicators in one pass ────────────────────────────────────────────────

def compute_all_indicators(
    df: pd.DataFrame,
    ema_fast: int = 9,
    ema_slow: int = 21,
    rsi_period: int = 7,
) -> pd.DataFrame:
    """
    Apply all indicators to a raw OHLCV DataFrame.
    Returns enriched DataFrame. Safe to call on full historical data.
    """
    df = add_emas(df, fast=ema_fast, slow=ema_slow)
    df = add_rsi(df, period=rsi_period)
    df = add_vwap(df)
    df = add_atr(df)
    return df


# ── Warm-up period check ──────────────────────────────────────────────────────

def indicators_ready(df: pd.DataFrame, ema_slow: int = 21) -> bool:
    """
    Returns True only when we have enough bars for all indicators
    to be reliable. Minimum = ema_slow + RSI period + buffer.
    """
    min_bars = ema_slow + 14  # ema_slow + ATR period
    if len(df) < min_bars:
        return False
    last = df.iloc[-1]
    return (
        pd.notna(last.get("ema9"))
        and pd.notna(last.get(f"ema{ema_slow}"))
        and pd.notna(last.get("rsi"))
        and pd.notna(last.get("vwap"))
    )

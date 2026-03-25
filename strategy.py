"""
strategy.py — Signal generation engine.
Stateless pure functions. No IBKR code. Identical logic used by
both live runner and backtester — guaranteed consistency.
"""

import pandas as pd
from typing import Optional, Dict, Any
from config import StrategyConfig
from indicators import indicators_ready


# ── Signal type ───────────────────────────────────────────────────────────────

LONG = "LONG"
SHORT = "SHORT"
NO_SIGNAL = None


# ── Core signal logic ─────────────────────────────────────────────────────────

def generate_signal(
    df: pd.DataFrame,
    cfg: StrategyConfig,
) -> Optional[str]:
    """
    Evaluate the last bar of df and return 'LONG', 'SHORT', or None.

    Entry Conditions
    ─────────────────
    LONG:
      1. EMA9 > EMA21                        (uptrend confirmed)
      2. EMA9 slope > threshold              (trend actively rising, not flat)
      3. Close is within VWAP zone           (pullback to value area)
      4. Close >= VWAP                       (price holding above VWAP)
      5. RSI between long_rsi_low and high   (neutral momentum, not extended)
      6. Volume >= min_volume_ratio of avg   (conviction bar, not dead air)

    SHORT:
      1. EMA9 < EMA21                        (downtrend confirmed)
      2. EMA9 slope < -threshold             (trend actively falling, not flat)
      3. Close is within VWAP zone           (pullback to value area)
      4. Close <= VWAP                       (price holding below VWAP)
      5. RSI between short_rsi_low and high  (neutral momentum)
      6. Volume >= min_volume_ratio of avg   (conviction bar)
    """
    if not indicators_ready(df, ema_slow=cfg.ema_slow):
        return NO_SIGNAL

    # Need at least 2 bars for slope calculation
    if len(df) < 2:
        return NO_SIGNAL

    bar  = df.iloc[-1]
    prev = df.iloc[-2]

    ema_fast_col = f"ema{cfg.ema_fast}"
    ema_slow_col = f"ema{cfg.ema_slow}"

    ema9    = bar[ema_fast_col]
    ema21   = bar[ema_slow_col]
    close   = bar["close"]
    vwap    = bar["vwap"]
    rsi_val = bar["rsi"]

    # Guard against NaN indicators
    if any(pd.isna(v) for v in [ema9, ema21, close, vwap, rsi_val]):
        return NO_SIGNAL

    # ── Volume filter — skip low conviction bars ──────────────────────────
    volume     = bar.get("volume", 0)
    avg_volume = df["volume"].iloc[-20:].mean()
    vol_ratio  = volume / avg_volume if avg_volume > 0 else 0
    if vol_ratio < cfg.min_volume_ratio:
        return NO_SIGNAL

    # ── EMA slope — trend must be actively moving, not flat ───────────────
    ema9_slope = bar[ema_fast_col] - prev[ema_fast_col]

    # ── VWAP zone: price within ±vwap_zone_pct of VWAP ───────────────────
    vwap_upper   = vwap * (1 + cfg.vwap_zone_pct)
    vwap_lower   = vwap * (1 - cfg.vwap_zone_pct)
    in_vwap_zone = vwap_lower <= close <= vwap_upper

    # ── LONG ──────────────────────────────────────────────────────────────
    if (
        ema9 > ema21                                            # Uptrend
        and ema9_slope > cfg.ema_slope_threshold                # Actively rising
        and in_vwap_zone                                        # Pullback to VWAP
        and close >= vwap                                       # Holding above VWAP
        and cfg.long_rsi_low <= rsi_val <= cfg.long_rsi_high    # RSI filter
    ):
        return LONG

    # ── SHORT ─────────────────────────────────────────────────────────────
    if (
        ema9 < ema21                                            # Downtrend
        and ema9_slope < -cfg.ema_slope_threshold               # Actively falling
        and in_vwap_zone                                        # Pullback to VWAP
        and close <= vwap                                       # Holding below VWAP
        and cfg.short_rsi_low <= rsi_val <= cfg.short_rsi_high  # RSI filter
    ):
        return SHORT

    return NO_SIGNAL


def get_signal_debug_info(df: pd.DataFrame, cfg: StrategyConfig) -> Dict[str, Any]:
    """
    Returns a dict of all indicator values and condition outcomes
    for the latest bar. Useful for logging and debugging signal states.
    """
    if len(df) < 2:
        return {}

    bar  = df.iloc[-1]
    prev = df.iloc[-2]

    ema_fast_col = f"ema{cfg.ema_fast}"
    ema_slow_col = f"ema{cfg.ema_slow}"

    ema9    = bar.get(ema_fast_col, float("nan"))
    ema21   = bar.get(ema_slow_col, float("nan"))
    close   = bar.get("close", float("nan"))
    vwap    = bar.get("vwap", float("nan"))
    rsi_val = bar.get("rsi", float("nan"))

    ema9_prev  = prev.get(ema_fast_col, float("nan"))
    ema9_slope = ema9 - ema9_prev if not pd.isna(ema9) and not pd.isna(ema9_prev) else float("nan")

    volume     = bar.get("volume", 0)
    avg_volume = df["volume"].iloc[-20:].mean()
    vol_ratio  = round(volume / avg_volume, 3) if avg_volume > 0 else 0

    vwap_upper = vwap * (1 + cfg.vwap_zone_pct) if not pd.isna(vwap) else float("nan")
    vwap_lower = vwap * (1 - cfg.vwap_zone_pct) if not pd.isna(vwap) else float("nan")

    return {
        "close":          round(close, 4),
        "ema9":           round(ema9, 4),
        "ema21":          round(ema21, 4),
        "ema9_slope":     round(ema9_slope, 4),
        "vwap":           round(vwap, 4),
        "vwap_upper":     round(vwap_upper, 4),
        "vwap_lower":     round(vwap_lower, 4),
        "rsi":            round(rsi_val, 2),
        "vol_ratio":      vol_ratio,
        "ema_trend":      "UP" if ema9 > ema21 else "DOWN",
        "slope_ok_long":  ema9_slope > cfg.ema_slope_threshold,
        "slope_ok_short": ema9_slope < -cfg.ema_slope_threshold,
        "in_vwap_zone":   vwap_lower <= close <= vwap_upper,
        "above_vwap":     close >= vwap,
        "below_vwap":     close <= vwap,
        "long_rsi_ok":    cfg.long_rsi_low <= rsi_val <= cfg.long_rsi_high,
        "short_rsi_ok":   cfg.short_rsi_low <= rsi_val <= cfg.short_rsi_high,
        "vol_ok":         vol_ratio >= cfg.min_volume_ratio,
        "signal":         generate_signal(df, cfg),
    }


def calculate_exit_prices(
    side: str,
    entry_price: float,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> Dict[str, float]:
    """
    Calculate bracket exit prices for a given entry.
    Returns {'tp': ..., 'sl': ...}
    """
    mult = 1 if side == LONG else -1
    tp = round(entry_price * (1 + mult * take_profit_pct), 2)
    sl = round(entry_price * (1 - mult * stop_loss_pct), 2)
    return {"tp": tp, "sl": sl}
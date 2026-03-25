"""
backtest.py — Offline backtester.
Uses IDENTICAL strategy.py, indicators.py, and risk.py logic as live trading.
Never maintain a separate strategy copy — they must stay in sync.

Usage:
    python backtest.py --csv data/spy_1min_hist.csv --start 2024-01-01 --end 2024-12-31
    python backtest.py  (uses default config and all available data)
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import numpy as np
try:
    import pytz
    ET = pytz.timezone("US/Eastern")
except ImportError:
    from datetime import timezone, timedelta
    ET = timezone(timedelta(hours=-5))  # Fallback (no DST, use pytz in production)

from config import config, AppConfig
from indicators import compute_all_indicators, indicators_ready
from strategy import generate_signal, get_signal_debug_info, calculate_exit_prices, LONG, SHORT
from risk import RiskManager


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_rth(ts: pd.Timestamp, cfg: AppConfig) -> bool:
    """Check if a timestamp is within RTH trading hours."""
    h, m = ts.hour, ts.minute
    h_cfg = cfg.hours
    after_open = (h, m) >= (h_cfg.market_open_hour, h_cfg.market_open_minute)
    before_cutoff = (h, m) <= (h_cfg.last_entry_hour, h_cfg.last_entry_minute)
    return after_open and before_cutoff


def hit_tp_or_sl(pos_side, high, low, tp, sl) -> Optional[str]:
    """Check if a bar's high/low hit TP or SL. Returns 'TP', 'SL', or None."""
    if pos_side == LONG:
        if high >= tp:
            return "TP"
        if low <= sl:
            return "SL"
    else:  # SHORT
        if low <= tp:
            return "TP"
        if high >= sl:
            return "SL"
    return None


# ── Main backtester ───────────────────────────────────────────────────────────

class Backtester:

    def __init__(self, cfg: AppConfig = None):
        self.cfg = cfg or config

    def run(
        self,
        csv_path: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        verbose: bool = False,
    ) -> pd.DataFrame:
        """
        Run the full backtest. Returns a DataFrame of completed trades.
        """
        print(f"\n{'='*60}")
        print(f"  IBKR SPY ALGO — BACKTEST ENGINE")
        print(f"{'='*60}")

        # ── Load data ─────────────────────────────────────────────────────
        if not Path(csv_path).exists():
            print(f"[ERROR] CSV file not found: {csv_path}")
            sys.exit(1)

        df = pd.read_csv(csv_path, parse_dates=["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        print(f"Loaded {len(df)} bars from {csv_path}")

        if start_date:
            df = df[df["timestamp"] >= start_date]
        if end_date:
            df = df[df["timestamp"] <= end_date]

        print(f"Testing on {len(df)} bars | "
              f"{df['timestamp'].min().date()} → {df['timestamp'].max().date()}")

        # ── Compute indicators ────────────────────────────────────────────
        sc = self.cfg.strategy
        df = compute_all_indicators(df, ema_fast=sc.ema_fast, ema_slow=sc.ema_slow, rsi_period=sc.rsi_period)
        df = df.reset_index(drop=True)

        # ── Run simulation ────────────────────────────────────────────────
        risk = RiskManager(self.cfg.risk)
        trades = []
        open_trade = None
        current_date = None
        bar_index = 0   # Intraday bar counter (for cooldown tracking)

        for i in range(len(df)):
            row = df.iloc[i]
            ts = pd.Timestamp(row["timestamp"])

            # Day boundary reset
            row_date = ts.date()
            if row_date != current_date:
                current_date = row_date
                risk.reset_daily()
                bar_index = 0
                if open_trade:
                    # EOD time stop — close any open position
                    open_trade["exit_price"] = row["close"]
                    open_trade["exit_reason"] = "EOD"
                    open_trade["exit_time"] = ts
                    pnl = self._calc_pnl(open_trade)
                    risk.record_trade(
                        bar_index, open_trade["side"],
                        open_trade["entry_price"], open_trade["exit_price"],
                        open_trade["shares"], "EOD"
                    )
                    trades.append({**open_trade, "pnl": pnl})
                    open_trade = None

            bar_index += 1

            # Skip non-RTH bars
            if not is_rth(ts, self.cfg):
                continue

            # ── Check open position ────────────────────────────────────────
            if open_trade:
                bars_held = bar_index - open_trade["bar_index"]

                # Check TP/SL on this bar
                outcome = hit_tp_or_sl(
                    open_trade["side"],
                    row["high"], row["low"],
                    open_trade["tp"], open_trade["sl"]
                )

                if outcome == "TP":
                    exit_price = open_trade["tp"]
                    exit_reason = "TP"
                elif outcome == "SL":
                    exit_price = open_trade["sl"]
                    exit_reason = "SL"
                elif bars_held >= self.cfg.exit.time_stop_bars:
                    exit_price = row["close"]
                    exit_reason = "TIME"
                else:
                    exit_price = None
                    exit_reason = None

                if exit_price is not None:
                    open_trade["exit_price"] = exit_price
                    open_trade["exit_reason"] = exit_reason
                    open_trade["exit_time"] = ts
                    pnl = self._calc_pnl(open_trade)
                    risk.record_trade(
                        bar_index, open_trade["side"],
                        open_trade["entry_price"], exit_price,
                        open_trade["shares"], exit_reason
                    )
                    trades.append({**open_trade, "pnl": pnl})

                    if verbose:
                        sign = "+" if pnl >= 0 else ""
                        print(
                            f"  [{ts.strftime('%Y-%m-%d %H:%M')}] "
                            f"CLOSE {open_trade['side']} | {exit_reason} | "
                            f"${open_trade['entry_price']:.2f}→${exit_price:.2f} | "
                            f"PnL: {sign}${pnl:.2f}"
                        )
                    open_trade = None

            # ── Look for new entry ─────────────────────────────────────────
            if open_trade is None:
                can_trade, reason = risk.can_trade(bar_index)
                if not can_trade:
                    continue

                sub_df = df.iloc[:i+1]
                if not indicators_ready(sub_df, ema_slow=sc.ema_slow):
                    continue

                signal = generate_signal(sub_df, sc)
                if signal is None:
                    continue

                entry_price = row["close"]
                shares = risk.position_size(entry_price)
                if shares <= 0:
                    continue

                exits = calculate_exit_prices(
                    signal, entry_price,
                    self.cfg.exit.stop_loss_pct,
                    self.cfg.exit.take_profit_pct,
                )

                open_trade = {
                    "side": signal,
                    "entry_price": entry_price,
                    "shares": shares,
                    "tp": exits["tp"],
                    "sl": exits["sl"],
                    "bar_index": bar_index,
                    "entry_time": ts,
                    "date": row_date,
                    "ema9": row.get("ema9", 0),
                    "ema21": row.get("ema21", 0),
                    "vwap": row.get("vwap", 0),
                    "rsi": row.get("rsi", 0),
                    # Placeholders
                    "exit_price": None,
                    "exit_reason": None,
                    "exit_time": None,
                    "pnl": None,
                }

                if verbose:
                    print(
                        f"  [{ts.strftime('%Y-%m-%d %H:%M')}] "
                        f"ENTRY {signal} | "
                        f"${entry_price:.2f} | TP:${exits['tp']:.2f} SL:${exits['sl']:.2f} | "
                        f"RSI:{row.get('rsi', 0):.1f}"
                    )

        results_df = pd.DataFrame(trades)
        self._print_report(results_df)
        self._save_report(results_df)
        return results_df

    def _calc_pnl(self, trade: dict) -> float:
        mult = 1 if trade["side"] == LONG else -1
        return (trade["exit_price"] - trade["entry_price"]) * mult * trade["shares"]

    def _print_report(self, df: pd.DataFrame):
        if df.empty:
            print("\n[No trades executed]")
            return

        wins   = df[df["pnl"] > 0]
        losses = df[df["pnl"] <= 0]
        net_pnl = df["pnl"].sum()
        gross_profit = wins["pnl"].sum()
        gross_loss   = abs(losses["pnl"].sum())
        win_rate     = len(wins) / len(df) * 100
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Exit breakdown
        exit_counts = df["exit_reason"].value_counts()

        # Daily stats
        df["date_str"] = pd.to_datetime(df["date"]).astype(str)
        daily_pnl = df.groupby("date_str")["pnl"].sum()
        max_dd_day = daily_pnl.min()

        print(f"\n{'='*60}")
        print(f"  BACKTEST RESULTS")
        print(f"{'='*60}")
        print(f"  Period:            {df['entry_time'].min().date()} → {df['entry_time'].max().date()}")
        print(f"  Total Trades:      {len(df)}")
        print(f"  Wins:              {len(wins)}  ({win_rate:.1f}%)")
        print(f"  Losses:            {len(losses)}")
        print(f"  Win Rate:          {win_rate:.1f}%  {'✅' if win_rate >= 60 else '❌'} (target: 60-70%)")
        print(f"  Profit Factor:     {profit_factor:.2f}  {'✅' if profit_factor >= 1.3 else '❌'} (target: >1.3)")
        print(f"  Net PnL:           ${net_pnl:.2f}")
        print(f"  Gross Profit:      ${gross_profit:.2f}")
        print(f"  Gross Loss:        ${gross_loss:.2f}")
        print(f"  Avg Win:           ${wins['pnl'].mean():.2f}" if not wins.empty else "  Avg Win:           N/A")
        print(f"  Avg Loss:          ${losses['pnl'].mean():.2f}" if not losses.empty else "  Avg Loss:          N/A")
        print(f"  Worst Day PnL:     ${max_dd_day:.2f}")
        print(f"  Exit TP:           {exit_counts.get('TP', 0)}")
        print(f"  Exit SL:           {exit_counts.get('SL', 0)}")
        print(f"  Exit TIME:         {exit_counts.get('TIME', 0)}")
        print(f"  Exit EOD:          {exit_counts.get('EOD', 0)}")
        print(f"\n  Monthly Return Est: ~{(net_pnl / self.cfg.risk.account_size) * 100:.1f}%")
        print(f"{'='*60}\n")

    def _save_report(self, df: pd.DataFrame):
        out_path = "logs/backtest_results.csv"
        Path("logs").mkdir(exist_ok=True)
        df.to_csv(out_path, index=False)
        print(f"Detailed trade log saved to: {out_path}")


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IBKR SPY Algo Backtester")
    parser.add_argument("--csv",   default="data/spy_1min_hist.csv", help="Path to 1-min OHLCV CSV")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--end",   default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--verbose", action="store_true", help="Print every trade")
    args = parser.parse_args()

    bt = Backtester()
    bt.run(csv_path=args.csv, start_date=args.start, end_date=args.end, verbose=args.verbose)

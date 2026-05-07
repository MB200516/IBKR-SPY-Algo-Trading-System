import csv
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

import pandas as pd
import pytz

from ib_insync import IB, Stock
from config import AppConfig
from indicators import compute_all_indicators

log = logging.getLogger("ibkr_algo")
ET  = pytz.timezone("US/Eastern")


class DataModule:
    """
    columns:
        timestamp, open, high, low, close, volume,
        vwap, ema9, ema21, rsi, atr

    Note on delayed data mode (use_delayed_data = True in IBKRConfig):
        IBKR market data type 3 (delayed) is set in runner.py immediately after
        connection via ib.reqMarketDataType(3). This module requires no changes
        to work with delayed data — IBKR transparently serves delayed bars
        through the same reqHistoricalData and reqRealTimeBars calls.
        Delayed data is 15–20 minutes behind real-time and is for testing only.
    """

    def __init__(self, cfg: AppConfig, ib: IB):
        self.cfg = cfg
        self.ib  = ib
        self._bars: list = []
        self._df:   Optional[pd.DataFrame] = None
        self._lock  = threading.Lock()
        self._on_new_bar: Optional[Callable] = None
        self._bar_sub = None

        # 5-sec → 1-min assembly state
        self._current_minute_bars  = []
        self._current_minute_start = None

    # ── Contract ─────────────────────────────────────────────────────────────

    def _contract(self) -> Stock:
        ic = self.cfg.instrument
        return Stock(ic.symbol, ic.exchange, ic.currency)

    # ── Historical warm-up ────────────────────────────────────────────────────

    def load_warmup_history(self, days: int = None):
        """
        Pull the last N days of 1-minute RTH bars from IBKR.
        Populates self._bars, rebuilds the indicator DataFrame,
        and saves raw history to CSV for backtesting.

        Works transparently with both live and delayed data modes.
        When use_delayed_data = True, IBKR returns delayed historical bars
        (set via reqMarketDataType(3) in runner.py before this call).
        """
        days = days or self.cfg.system.warmup_days

        data_mode = (
            "delayed (testing only)"
            if self.cfg.ibkr.use_delayed_data
            else "live"
        )
        log.info(
            f"Requesting {days} days of 1-min historical data "
            f"for {self.cfg.instrument.symbol} "
            f"[market data mode: {data_mode}]..."
        )

        contract = self._contract()
        bars = self.ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=f"{days} D",
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )

        if not bars:
            log.warning("No historical data received. Proceeding with empty history.")
            if self.cfg.ibkr.use_delayed_data:
                log.warning(
                    "Delayed data mode is active — if no bars were returned, "
                    "ensure TWS is connected and reqMarketDataType(3) was accepted. "
                    "Some paper accounts may still require a data subscription for "
                    "real-time bars even in delayed mode."
                )
            return

        raw = []
        for b in bars:
            # FIX: b.date from IBKR is a string — parse it directly
            raw.append({
                "timestamp": pd.Timestamp(b.date),
                "open":      b.open,
                "high":      b.high,
                "low":       b.low,
                "close":     b.close,
                "volume":    b.volume,
            })

        df = pd.DataFrame(raw)
        df = df.sort_values("timestamp").reset_index(drop=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"])

        # Save raw historical data to CSV
        self._save_to_csv(df)

        with self._lock:
            self._bars = df.to_dict("records")
            self._rebuild_df()

        log.info(f"Historical warm-up complete: {len(raw)} bars loaded.")

    # ── Live bar subscription ─────────────────────────────────────────────────

    def subscribe_live_bars(self, on_new_bar: Callable = None):
        """
        Subscribe to real-time 5-second bars from IBKR.
        Assembled into 1-minute bars internally.
        on_new_bar(df) is called each time a 1-minute bar is finalized.

        When use_delayed_data = True, IBKR will serve delayed bars here.
        The assembly logic is identical for both live and delayed modes.
        """
        self._on_new_bar           = on_new_bar
        self._current_minute_bars  = []
        self._current_minute_start = None

        contract = self._contract()
        bars = self.ib.reqRealTimeBars(
            contract,
            barSize=5,
            whatToShow="TRADES",
            useRTH=True,
        )
        bars.updateEvent += self._on_5sec_bar
        self._bar_sub = bars
        log.info(
            "Live 5-second bar subscription active "
            f"[{'delayed data — testing only' if self.cfg.ibkr.use_delayed_data else 'live data'}]."
        )

    def _on_5sec_bar(self, bars, hasNewBar):
        """
        IBKR delivers 5-second bars. Assemble into 1-minute bars.
        Called in the ib event loop thread.
        """
        if not hasNewBar or not bars:
            return

        bar = bars[-1]

        # FIX: IBKR real-time bars deliver bar.time as a Unix int.
        # MockIB delivers it as a pd.Timestamp. Handle both.
        try:
            if isinstance(bar.time, (int, float)):
                bar_time = pd.Timestamp(bar.time, unit="s").tz_localize("UTC").tz_convert(ET)
            else:
                bar_time = pd.Timestamp(bar.time)
                if bar_time.tzinfo is None:
                    bar_time = ET.localize(bar_time)
        except Exception as e:
            log.warning(f"Could not parse bar time '{bar.time}': {e}")
            return

        bar_minute = bar_time.replace(second=0, microsecond=0)

        if self._current_minute_start is None:
            self._current_minute_start = bar_minute

        if bar_minute == self._current_minute_start:
            self._current_minute_bars.append(bar)
        else:
            if self._current_minute_bars:
                self._finalize_minute_bar(
                    self._current_minute_start,
                    self._current_minute_bars
                )
            self._current_minute_start = bar_minute
            self._current_minute_bars  = [bar]

    def _finalize_minute_bar(self, minute_start, five_sec_bars):
        """
        Collapse a list of 5-second bars into a single 1-minute OHLCV bar.
        Update the DataFrame and call on_new_bar callback.
        """
        o = five_sec_bars[0].open
        h = max(b.high   for b in five_sec_bars)
        l = min(b.low    for b in five_sec_bars)
        c = five_sec_bars[-1].close
        v = sum(b.volume for b in five_sec_bars)

        new_bar = {
            "timestamp": minute_start,
            "open":      o,
            "high":      h,
            "low":       l,
            "close":     c,
            "volume":    v,
        }

        with self._lock:
            self._bars.append(new_bar)
            self._rebuild_df()
            df_snapshot = self._df.copy() if self._df is not None else None

        self._append_bar_to_csv(new_bar)

        log.debug(
            f"Bar [{minute_start.strftime('%H:%M')}] "
            f"O:{o:.2f} H:{h:.2f} L:{l:.2f} C:{c:.2f} V:{v}"
        )

        if self._on_new_bar and df_snapshot is not None:
            self._on_new_bar(df_snapshot)

    def _rebuild_df(self):
        """
        Rebuild the full indicator DataFrame from raw bars.
        Must be called inside self._lock.
        """
        if not self._bars:
            self._df = None
            return

        df = pd.DataFrame(self._bars)
        df = df.sort_values("timestamp").reset_index(drop=True)

        cfg = self.cfg.strategy
        df  = compute_all_indicators(
            df,
            ema_fast=cfg.ema_fast,
            ema_slow=cfg.ema_slow,
            rsi_period=cfg.rsi_period,
        )
        self._df = df

    # ── Public accessors ──────────────────────────────────────────────────────

    def get_dataframe(self) -> Optional[pd.DataFrame]:
        """Returns a snapshot of the current indicator DataFrame."""
        with self._lock:
            return self._df.copy() if self._df is not None else None

    def get_latest_bar(self) -> Optional[dict]:
        """Returns the most recent completed bar as a dict."""
        with self._lock:
            if self._df is None or len(self._df) == 0:
                return None
            return self._df.iloc[-1].to_dict()

    def bar_count(self) -> int:
        with self._lock:
            return len(self._bars)

    def reset_intraday(self):
        """
        Call at 09:30 each morning.
        Clears prior-day bars so VWAP resets correctly.
        """
        with self._lock:
            today = datetime.now(ET).date()
            kept  = []
            for b in self._bars:
                ts = pd.Timestamp(b["timestamp"])
                # FIX: Strip timezone before .date() comparison
                if ts.tzinfo is not None:
                    ts = ts.tz_localize(None)
                if ts.date() == today:
                    kept.append(b)
            self._bars = kept
            self._rebuild_df()
        log.info(f"Intraday reset complete — {len(self._bars)} bars retained for today.")

    # ── CSV storage ───────────────────────────────────────────────────────────

    def _save_to_csv(self, df: pd.DataFrame):
        """Save full historical DataFrame to CSV (overwrites existing file)."""
        path = self.cfg.system.hist_data_file
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False)
        log.debug(f"Historical data saved to {path} ({len(df)} rows)")

    def _append_bar_to_csv(self, bar: dict):
        """
        Append a single completed live bar to the history CSV.
        FIX: Checks file size not just existence to avoid missing headers.
        FIX: import csv moved to module level — not repeated per-call.
        """
        path   = self.cfg.system.hist_data_file
        p      = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        is_new = not p.exists() or p.stat().st_size == 0

        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["timestamp", "open", "high", "low", "close", "volume"]
            )
            if is_new:
                writer.writeheader()
            writer.writerow(
                {k: bar[k] for k in ["timestamp", "open", "high", "low", "close", "volume"]}
            )

    def unsubscribe(self):
        """Cancel live bar subscription cleanly on shutdown."""
        if self._bar_sub is not None:
            try:
                self.ib.cancelRealTimeBars(self._bar_sub)
                log.info("Real-time bar subscription cancelled.")
            except Exception as e:
                log.warning(f"Error cancelling bar subscription: {e}")
            finally:
                self._bar_sub = None

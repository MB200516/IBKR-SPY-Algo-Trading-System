"""
runner.py — Main orchestrator / wiring layer.
Connects all modules together. Handles:
  - IBKR connection + reconnect logic
  - Market hours gating
  - Signal → risk check → order submission pipeline
  - Trade close callback → logger + risk manager update
  - Graceful shutdown (Ctrl+C or kill switch)
  - Daily reset at 09:30

Modes:
  "readonly" → Print signals only, no orders
  "paper"    → Full execution on paper account
  "live"     → Full execution on live account (requires explicit config change)

Usage:
    python runner.py              # Uses config.py system.mode
    python runner.py --mode paper
    python runner.py --mode readonly
"""

import argparse
import logging
import signal
import sys
import time
import threading
from datetime import datetime, date
from typing import Optional

import pytz
from ib_insync import IB

from config import config, AppConfig
from data import DataModule
from execution import ExecutionEngine, OpenPosition
from risk import RiskManager
from logger import TradeLogger, setup_logging
from strategy import generate_signal, get_signal_debug_info

ET = pytz.timezone("US/Eastern")


class AlgoRunner:
    """
    Top-level orchestrator. Instantiate once and call .start().
    """

    def __init__(self, cfg: AppConfig = None, mode_override: str = None):
        self.cfg = cfg or config
        if mode_override:
            self.cfg.system.mode = mode_override

        # Setup logging first
        self._app_log = setup_logging(self.cfg.system)
        self._log = logging.getLogger("ibkr_algo")

        self._trade_logger = TradeLogger(self.cfg.system)
        self._risk = RiskManager(self.cfg.risk)

        self._ib = IB()
        self._data: Optional[DataModule] = None
        self._exec: Optional[ExecutionEngine] = None

        self._running = False
        self._bar_index = 0
        self._shutdown_event = threading.Event()
        self._current_date: Optional[date] = None

        # Register OS signal handlers (Ctrl+C, SIGTERM)
        signal.signal(signal.SIGINT,  self._handle_os_signal)
        signal.signal(signal.SIGTERM, self._handle_os_signal)

    # ── Connection ────────────────────────────────────────────────────────────

    def _connect(self) -> bool:
        """Attempt to connect to TWS. Returns True on success."""
        ib_cfg = self.cfg.ibkr
        self._log.info(
            f"Connecting to IBKR TWS @ {ib_cfg.host}:{ib_cfg.port} "
            f"(clientId={ib_cfg.client_id}, mode={self.cfg.system.mode.upper()})"
        )
        try:
            self._ib.connect(
                ib_cfg.host, ib_cfg.port,
                clientId=ib_cfg.client_id,
                timeout=ib_cfg.timeout
            )
            account = self._ib.managedAccounts()[0] if self._ib.managedAccounts() else "unknown"
            self._log.info(f"Connected. Account: {account}")
            self._trade_logger.log_connection("CONNECTED", f"Account: {account}")
            return True
        except Exception as e:
            self._log.error(f"Connection failed: {e}")
            return False

    def _reconnect_loop(self) -> bool:
        """
        Called when connection drops. Retries with exponential backoff.
        Blocks until reconnected or max attempts reached.
        """
        cfg = self.cfg.system
        for attempt in range(1, cfg.max_reconnect_attempts + 1):
            self._trade_logger.log_reconnect_attempt(attempt, cfg.max_reconnect_attempts)
            delay = min(cfg.reconnect_delay_seconds * attempt, 60)
            time.sleep(delay)

            try:
                self._ib.disconnect()
            except Exception:
                pass

            if self._connect():
                self._log.info("Reconnected successfully.")
                self._trade_logger.log_connection("RECONNECTED")
                return True

        self._log.critical("Max reconnect attempts reached. Shutting down.")
        self._shutdown()
        return False

    def _is_connected(self) -> bool:
        return self._ib.isConnected()

    # ── Market hours helpers ──────────────────────────────────────────────────

    def _now_et(self) -> datetime:
        return datetime.now(ET)

    def _is_market_open(self) -> bool:
        now = self._now_et()
        h, m = now.hour, now.minute
        hc = self.cfg.hours
        return (
            now.weekday() < 5   # Mon–Fri only
            and (h, m) >= (hc.market_open_hour, hc.market_open_minute)
            and (h, m) < (hc.market_close_hour, hc.market_close_minute)
        )

    def _past_last_entry(self) -> bool:
        now = self._now_et()
        h, m = now.hour, now.minute
        hc = self.cfg.hours
        return (h, m) > (hc.last_entry_hour, hc.last_entry_minute)

    def _is_market_day(self) -> bool:
        return self._now_et().weekday() < 5

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def _on_new_bar(self, df):
        """
        Called by DataModule every time a new 1-minute bar is finalized.
        This is the main trading loop — called ~1x per minute during RTH.
        """
        if self._shutdown_event.is_set():
            return

        self._bar_index += 1
        now = self._now_et()

        # ── Day boundary reset ────────────────────────────────────────────
        today = now.date()
        if today != self._current_date:

            # FIX: Write PREVIOUS day's summary before resetting state
            if self._current_date is not None:
                prev_stats = self._risk.session_stats()
                self._trade_logger.log_daily_summary(
                    date=self._current_date.strftime("%Y-%m-%d"),
                    stats=prev_stats,
                    kill_switch_triggered=self._risk.kill_switch_active,
                    kill_switch_reason=self._risk.kill_switch_reason,
                )
                self._log.info(
                    f"Day closed: {self._current_date} | "
                    f"Trades: {prev_stats.get('total_trades', 0)} | "
                    f"Net PnL: ${prev_stats.get('net_pnl', 0):.2f} | "
                    f"Win Rate: {prev_stats.get('win_rate', 0):.1f}%"
                )

            self._current_date = today
            self._log.info(f"=== New trading day: {today} ===")
            self._risk.reset_daily()
            self._bar_index = 1

            if self._data:
                self._data.reset_intraday()

        # Not in trading hours → skip everything below
        if not self._is_market_open():
            return

        # FIX: Only check time stop during market hours
        if self._exec and self.cfg.system.mode != "readonly" and self._is_market_open():
            self._exec.update_bar_index(self._bar_index)
            self._exec.check_time_stop(self._bar_index)

        # ── Signal evaluation ─────────────────────────────────────────────

        # No new entries after cutoff time
        if self._past_last_entry():
            return

        # No new entries if position already open
        if self._exec and self._exec.has_open_position:
            return

        # Risk gate
        can_trade, reason = self._risk.can_trade(self._bar_index)
        if not can_trade:
            self._trade_logger.log_risk_block(reason)
            if self._risk.kill_switch_active:
                self._trade_logger.log_kill_switch(reason)
            return

        # Signal check
        signal = generate_signal(df, self.cfg.strategy)
        debug_info = get_signal_debug_info(df, self.cfg.strategy)

        if signal is None:
            return

        self._trade_logger.log_signal(signal, debug_info)

        # ── Readonly mode ─────────────────────────────────────────────────
        if self.cfg.system.mode == "readonly":
            self._log.info(f"[READONLY] Signal: {signal} — no order placed")
            return

        # ── Paper / Live: place order ─────────────────────────────────────
        latest = df.iloc[-1]
        entry_price = latest["close"]
        shares = self._risk.position_size(entry_price)

        if shares <= 0:
            self._log.warning(f"Position size = 0 at ${entry_price:.2f} — skipping")
            return

        position = self._exec.place_bracket_order(
            side=signal,
            shares=shares,
            entry_price=entry_price,
            bar_index=self._bar_index,
            bar_indicators=debug_info,
        )

        if position:
            from strategy import calculate_exit_prices
            exits = calculate_exit_prices(
                signal, entry_price,
                self.cfg.exit.stop_loss_pct,
                self.cfg.exit.take_profit_pct
            )
            self._trade_logger.log_entry(signal, entry_price, shares, exits["tp"], exits["sl"])

    def _on_trade_closed(self, pos: OpenPosition, exit_price: float, exit_reason: str):
        """
        Called by ExecutionEngine when a position closes (TP, SL, TIME, MANUAL).
        Updates risk manager and writes trade log.
        """
        entry_price = pos.filled_entry_price or pos.entry_price
        pnl = self._risk.record_trade(
            bar_index=self._bar_index,
            side=pos.side,
            entry_price=entry_price,
            exit_price=exit_price,
            shares=pos.shares,
            exit_reason=exit_reason,
        )

        self._trade_logger.log_trade(
            entry_time=pos.entry_time,
            exit_time=self._now_et(),
            side=pos.side,
            entry_price=entry_price,
            exit_price=exit_price,
            shares=pos.shares,
            pnl=pnl,
            exit_reason=exit_reason,
            bar_indicators=pos.entry_bar_indicators,
            daily_pnl_after=self._risk.daily_pnl,
            trades_today=self._risk.trades_today,
        )

        # Log kill switch if it just triggered
        if self._risk.kill_switch_active:
            self._trade_logger.log_kill_switch(self._risk.kill_switch_reason)

    # ── Startup ───────────────────────────────────────────────────────────────

    def start(self):
        """Entry point. Connects, warms up, starts live bar loop."""
        self._log.info("=" * 55)
        self._log.info("  IBKR SPY ALGO TRADING SYSTEM — STARTING")
        self._log.info(f"  Mode: {self.cfg.system.mode.upper()}")
        self._log.info(f"  Account Size: ${self.cfg.risk.account_size:,.0f}")
        self._log.info(f"  Risk/Trade: ${self.cfg.risk.risk_per_trade_dollars:.0f} | "
                       f"Daily Limit: ${self.cfg.risk.daily_loss_limit_dollars:.0f}")
        self._log.info("=" * 55)

        # Connect
        if not self._connect():
            self._log.critical("Cannot connect to TWS. Ensure TWS is running with API enabled.")
            sys.exit(1)

        # Wire up disconnection handler
        self._ib.disconnectedEvent += self._on_disconnected

        # Initialize modules
        self._data = DataModule(self.cfg, self._ib)
        self._exec = ExecutionEngine(self.cfg, self._ib)
        self._exec.set_on_trade_closed(self._on_trade_closed)

        # Clear orphaned orders from previous session
        self._exec.cancel_all_open_orders()

        # Historical warm-up
        if self._is_market_day():
            self._data.load_warmup_history()
        else:
            self._log.info("Weekend/holiday — skipping historical warm-up.")
            self._log.warning(
                "Indicators will not be ready until ~35 live bars "
                "accumulate on next market open."
            )

        self._risk.reset_daily()
        self._running = True
        self._current_date = self._now_et().date()

        # Subscribe to live bars
        self._data.subscribe_live_bars(on_new_bar=self._on_new_bar)
        self._log.info("Live bar subscription active. Awaiting market data...")

        # Main event loop
        try:
            while self._running and not self._shutdown_event.is_set():
                self._ib.sleep(1)

                # Connection watchdog
                if not self._is_connected():
                    self._log.warning("Connection lost. Starting reconnect loop...")

                    # FIX: Unsubscribe before reconnecting to prevent duplicate callbacks
                    if self._data:
                        self._data.unsubscribe()

                    if not self._reconnect_loop():
                        break

                    # Re-subscribe after successful reconnect
                    self._data.subscribe_live_bars(on_new_bar=self._on_new_bar)

        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _on_disconnected(self):
        self._log.warning("IBKR disconnected event received.")

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def _handle_os_signal(self, signum, frame):
        self._log.info(f"\nShutdown signal received ({signum}). Stopping gracefully...")
        self._shutdown()

    def _shutdown(self):
        # Guard against double-call
        if not self._running:
            return
        self._running = False
        self._shutdown_event.set()

        self._log.info("Shutting down...")

        # Close any open position gracefully
        if self._exec and self._exec.has_open_position:
            self._log.warning("Open position detected on shutdown — sending market close.")
            self._exec.manual_close_all()
            time.sleep(2)

        # Unsubscribe data feeds
        if self._data:
            self._data.unsubscribe()

        # Write today's daily summary
        today = self._now_et().strftime("%Y-%m-%d")
        stats = self._risk.session_stats()
        self._trade_logger.log_daily_summary(
            date=today,
            stats=stats,
            kill_switch_triggered=self._risk.kill_switch_active,
            kill_switch_reason=self._risk.kill_switch_reason,
        )

        # Disconnect from IBKR
        try:
            self._ib.disconnect()
        except Exception:
            pass

        self._log.info("Shutdown complete.")


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IBKR SPY Algo Runner")
    parser.add_argument(
        "--mode",
        choices=["readonly", "paper", "live"],
        default=None,
        help="Override system mode (readonly/paper/live)",
    )
    args = parser.parse_args()

    runner = AlgoRunner(mode_override=args.mode)
    runner.start()
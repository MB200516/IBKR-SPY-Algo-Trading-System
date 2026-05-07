"""
logger.py — Logging and reporting module.
Writes trade logs, daily summaries, and error logs.
Console output uses Python logging module with clean formatting.
"""

import csv
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any
import threading

import pytz

from config import SystemConfig, AppConfig


# ── Console + file logger setup ──────────────────────────────────────────────

def setup_logging(cfg: SystemConfig) -> logging.Logger:
    """
    Configure the root logger:
    - Console: INFO level, clean format
    - File: WARNING and above → errors.log
    """
    Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)

    level = getattr(logging, cfg.log_level.upper(), logging.INFO)

    logger = logging.getLogger("ibkr_algo")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(ch)

    # File handler (errors only)
    fh = logging.FileHandler(cfg.error_log_file, mode="a", encoding="utf-8")
    fh.setLevel(logging.WARNING)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)

    return logger


# ── Trade Logger ─────────────────────────────────────────────────────────────

TRADE_CSV_HEADERS = [
    "trade_id", "date", "entry_time", "exit_time", "side",
    "entry_price", "exit_price", "shares", "pnl", "pnl_pct",
    "exit_reason", "ema9", "ema21", "vwap", "rsi",
    "daily_pnl_after", "trades_today",
]

DAILY_CSV_HEADERS = [
    "date", "total_trades", "wins", "losses", "win_rate_pct",
    "gross_profit", "gross_loss", "net_pnl", "profit_factor",
    "max_consecutive_losses", "kill_switch_triggered", "kill_switch_reason",
]


class TradeLogger:
    """
    Thread-safe CSV trade logger + daily summary writer.
    """

    def __init__(self, cfg: SystemConfig):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._trade_counter = 0
        self._log = logging.getLogger("ibkr_algo")
        self._ensure_files()

    def _ensure_files(self):
        """Create CSV files with headers if they don't exist."""
        Path(self.cfg.log_dir).mkdir(parents=True, exist_ok=True)
        Path(self.cfg.data_dir).mkdir(parents=True, exist_ok=True)

        if not Path(self.cfg.trade_log_file).exists():
            self._write_csv_row(self.cfg.trade_log_file, TRADE_CSV_HEADERS, header=True)

        if not Path(self.cfg.daily_summary_file).exists():
            self._write_csv_row(self.cfg.daily_summary_file, DAILY_CSV_HEADERS, header=True)

    def log_trade(
        self,
        entry_time: datetime,
        exit_time: datetime,
        side: str,
        entry_price: float,
        exit_price: float,
        shares: int,
        pnl: float,
        exit_reason: str,
        bar_indicators: Dict[str, Any],
        daily_pnl_after: float,
        trades_today: int,
    ):
        """Write one completed trade to trades.csv and log to console."""
        with self._lock:
            self._trade_counter += 1
            trade_id = self._trade_counter

        pnl_pct = (pnl / (entry_price * shares)) * 100 if entry_price and shares else 0

        row = [
            trade_id,
            entry_time.strftime("%Y-%m-%d"),
            entry_time.strftime("%H:%M:%S"),
            exit_time.strftime("%H:%M:%S"),
            side,
            round(entry_price, 4),
            round(exit_price, 4),
            shares,
            round(pnl, 2),
            round(pnl_pct, 3),
            exit_reason,
            round(bar_indicators.get("ema9", 0), 4),
            round(bar_indicators.get("ema21", 0), 4),
            round(bar_indicators.get("vwap", 0), 4),
            round(bar_indicators.get("rsi", 0), 2),
            round(daily_pnl_after, 2),
            trades_today,
        ]
        self._write_csv_row(self.cfg.trade_log_file, row)

        pnl_sign = "+" if pnl >= 0 else ""
        self._log.info(
            f"TRADE #{trade_id} CLOSED | {side} | "
            f"Entry: ${entry_price:.2f} → Exit: ${exit_price:.2f} | "
            f"Shares: {shares} | PnL: {pnl_sign}${pnl:.2f} | "
            f"Reason: {exit_reason} | Daily PnL: {pnl_sign}${daily_pnl_after:.2f}"
        )

    def log_daily_summary(
        self,
        date: str,
        stats: Dict[str, Any],
        kill_switch_triggered: bool,
        kill_switch_reason: str,
    ):
        """Write end-of-day summary row."""
        row = [
            date,
            stats.get("total_trades", 0),
            stats.get("wins", 0),
            stats.get("losses", 0),
            stats.get("win_rate", 0),
            stats.get("gross_profit", 0),
            stats.get("gross_loss", 0),
            stats.get("net_pnl", 0),
            stats.get("profit_factor", 0),
            stats.get("max_consecutive_losses", 0),
            kill_switch_triggered,
            kill_switch_reason,
        ]
        self._write_csv_row(self.cfg.daily_summary_file, row)

        self._log.info(
            f"=== DAILY SUMMARY [{date}] === "
            f"Trades: {stats.get('total_trades', 0)} | "
            f"Win Rate: {stats.get('win_rate', 0):.1f}% | "
            f"Net PnL: ${stats.get('net_pnl', 0):.2f} | "
            f"Profit Factor: {stats.get('profit_factor', 0):.2f}"
        )

    def log_signal(self, signal: str, bar_info: Dict[str, Any]):
        """Log a generated signal (read-only mode or pre-trade log)."""
        self._log.info(
            f"SIGNAL: {signal} | "
            f"Close: ${bar_info.get('close', 0):.2f} | "
            f"VWAP: ${bar_info.get('vwap', 0):.2f} | "
            f"EMA9: ${bar_info.get('ema9', 0):.2f} | "
            f"EMA21: ${bar_info.get('ema21', 0):.2f} | "
            f"RSI: {bar_info.get('rsi', 0):.1f}"
        )

    def log_risk_block(self, reason: str):
        self._log.info(f"TRADE BLOCKED: {reason}")

    def log_entry(self, side: str, price: float, shares: int, tp: float, sl: float):
        self._log.info(
            f"ORDER PLACED | {side} {shares} shares @ ${price:.2f} | "
            f"TP: ${tp:.2f} | SL: ${sl:.2f}"
        )

    def log_kill_switch(self, reason: str):
        self._log.warning(f" KILL SWITCH ACTIVATED: {reason}")

    def log_connection(self, status: str, detail: str = ""):
        msg = f"CONNECTION: {status}"
        if detail:
            msg += f" — {detail}"
        self._log.info(msg)

    def log_reconnect_attempt(self, attempt: int, max_attempts: int):
        self._log.warning(
            f"RECONNECT attempt {attempt}/{max_attempts}..."
        )

    def _write_csv_row(self, filepath: str, row: list, header: bool = False):
        """Append a row to a CSV file. Thread-safe via lock."""
        with self._lock:
            mode = "w" if header else "a"
            with open(filepath, mode, newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(row)

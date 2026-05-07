"""
risk.py — Risk Manager.
Manages position sizing, daily loss kill-switch, consecutive loss tracking,
cooldown enforcement, and all gate checks before a trade is allowed.
"""

import threading
from dataclasses import dataclass, field
from typing import Tuple, Optional
from config import RiskConfig


@dataclass
class TradeRecord:
    bar_index: int
    side: str
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    exit_reason: str   # "TP", "SL", "TIME", "MANUAL"


class RiskManager:
    """
    Thread-safe risk manager. All state mutations are protected by a lock
    so the execution engine can call record_trade() from a callback thread.
    """

    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self._lock = threading.Lock()

        # Daily state (reset at market open each day)
        self._daily_pnl: float = 0.0
        self._trades_today: int = 0
        self._consecutive_losses: int = 0
        self._last_trade_bar: int = -9999
        self._kill_switch_active: bool = False
        self._kill_switch_reason: str = ""

        # Session history
        self._trade_history: list = []

    # ── Gate check ────────────────────────────────────────────────────────────

    def can_trade(self, current_bar_index: int) -> Tuple[bool, str]:
        """
        Returns (True, "OK") if a new trade is allowed.
        Returns (False, reason_string) if blocked.
        Call this before generating signals.
        """
        with self._lock:
            if self._kill_switch_active:
                return False, f"Kill switch: {self._kill_switch_reason}"

            # Daily loss limit
            loss_limit = self.cfg.daily_loss_limit_dollars
            if self._daily_pnl <= -loss_limit:
                self._activate_kill_switch(f"Daily loss limit hit (${self._daily_pnl:.2f})")
                return False, self._kill_switch_reason

            # Max trades per day
            if self._trades_today >= self.cfg.max_trades_per_day:
                return False, f"Max trades/day reached ({self._trades_today})"

            # Consecutive losses
            if self._consecutive_losses >= self.cfg.max_consecutive_losses:
                return False, f"Max consecutive losses ({self._consecutive_losses}) reached — paused"

            # Cooldown after last trade
            bars_since_last = current_bar_index - self._last_trade_bar
            if bars_since_last < self.cfg.cooldown_bars:
                return False, f"Cooldown: {self.cfg.cooldown_bars - bars_since_last} bars remaining"

            return True, "OK"

    # ── Position sizing ───────────────────────────────────────────────────────

    def position_size(self, entry_price: float) -> int:
        """
        Shares = Risk$ / (Stop Loss$ per share)
        Capped by max notional and floored at 1.

        With $10,000 account, 0.2% risk, 0.20% SL on SPY ~$490:
        Risk$ = $20, SL per share = $0.98, Shares = 20 (capped by notional)
        """
        risk_dollars = self.cfg.risk_per_trade_dollars
        from config import config
        stop_per_share = entry_price * config.exit.stop_loss_pct
        if stop_per_share <= 0:
            return 0

        shares_by_risk = int(risk_dollars / stop_per_share)
        shares_by_notional = int(self.cfg.max_notional_per_trade / entry_price)

        shares = min(shares_by_risk, shares_by_notional)
        return max(shares, 0)

    # ── Record completed trade ────────────────────────────────────────────────

    def record_trade(
        self,
        bar_index: int,
        side: str,
        entry_price: float,
        exit_price: float,
        shares: int,
        exit_reason: str,
    ) -> float:
        """
        Record outcome of a closed trade.
        Updates daily PnL, consecutive loss counter, trade count.
        Returns realized PnL.
        """
        mult = 1 if side == "LONG" else -1
        pnl = (exit_price - entry_price) * mult * shares

        with self._lock:
            self._daily_pnl += pnl
            self._trades_today += 1
            self._last_trade_bar = bar_index

            if pnl < 0:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0  # Reset on any win

            self._trade_history.append(TradeRecord(
                bar_index=bar_index,
                side=side,
                entry_price=entry_price,
                exit_price=exit_price,
                shares=shares,
                pnl=pnl,
                exit_reason=exit_reason,
            ))

            # Auto-trigger kill switch if daily limit hit
            if self._daily_pnl <= -self.cfg.daily_loss_limit_dollars:
                self._activate_kill_switch(
                    f"Daily loss limit hit after trade (${self._daily_pnl:.2f})"
                )

        return pnl

    # ── Kill switch ───────────────────────────────────────────────────────────

    def _activate_kill_switch(self, reason: str):
        """Internal. Call only from within lock."""
        self._kill_switch_active = True
        self._kill_switch_reason = reason

    def force_kill_switch(self, reason: str = "Manual override"):
        """External call to manually activate kill switch."""
        with self._lock:
            self._activate_kill_switch(reason)

    def reset_kill_switch(self):
        """Only for testing or manual override. NOT called automatically."""
        with self._lock:
            self._kill_switch_active = False
            self._kill_switch_reason = ""

    # ── Daily reset ───────────────────────────────────────────────────────────

    def reset_daily(self):
        """
        Call at 09:30 ET each morning (or on runner startup).
        Resets all daily counters. Does NOT reset session history.
        """
        with self._lock:
            self._daily_pnl = 0.0
            self._trades_today = 0
            self._consecutive_losses = 0
            self._last_trade_bar = -9999
            self._kill_switch_active = False
            self._kill_switch_reason = ""

    # ── Status snapshot ───────────────────────────────────────────────────────

    @property
    def daily_pnl(self) -> float:
        with self._lock:
            return self._daily_pnl

    @property
    def trades_today(self) -> int:
        with self._lock:
            return self._trades_today

    @property
    def consecutive_losses(self) -> int:
        with self._lock:
            return self._consecutive_losses

    @property
    def kill_switch_active(self) -> bool:
        with self._lock:
            return self._kill_switch_active

    @property
    def kill_switch_reason(self) -> str:
        with self._lock:
            return self._kill_switch_reason

    def status_snapshot(self) -> dict:
        """Returns a dict of current risk state for logging."""
        with self._lock:
            return {
                "daily_pnl": round(self._daily_pnl, 2),
                "trades_today": self._trades_today,
                "consecutive_losses": self._consecutive_losses,
                "kill_switch": self._kill_switch_active,
                "kill_switch_reason": self._kill_switch_reason,
                "daily_loss_limit": -self.cfg.daily_loss_limit_dollars,
                "remaining_loss_budget": round(
                    self.cfg.daily_loss_limit_dollars + self._daily_pnl, 2
                ),
            }

    def session_stats(self) -> dict:
        """Compute win rate, profit factor, net PnL from session history."""
        trades = self._trade_history
        if not trades:
            return {"total_trades": 0}

        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        gross_profit = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "net_pnl": round(sum(t.pnl for t in trades), 2),
            "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else float("inf"),
            "avg_win": round(gross_profit / len(wins), 2) if wins else 0,
            "avg_loss": round(gross_loss / len(losses), 2) if losses else 0,
        }

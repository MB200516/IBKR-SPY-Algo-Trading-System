"""
execution.py — Order execution engine.
Handles IBKR order placement, bracket/OCA orders, fill tracking,
position management, and time-stop enforcement.
Uses ib_insync for clean async-compatible API access.
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable, Dict, Tuple

import pytz
from ib_insync import IB, Stock, Order, Trade, Fill, Contract, util

from config import AppConfig
from strategy import calculate_exit_prices, LONG, SHORT

log = logging.getLogger("ibkr_algo")
ET = pytz.timezone("US/Eastern")


@dataclass
class OpenPosition:
    """Tracks a currently open bracket position."""
    side: str
    entry_price: float
    shares: int
    entry_time: datetime
    bar_index: int
    tp_price: float
    sl_price: float
    parent_order_id: int
    tp_order_id: int
    sl_order_id: int
    entry_bar_indicators: Dict = field(default_factory=dict)
    filled_entry_price: Optional[float] = None   # Actual fill price (may differ from signal price)


class ExecutionEngine:
    """
    Manages the lifecycle of a single open position at a time.

    Order structure (bracket / OCA):
    ─────────────────────────────────
    Parent  → Market order (entry)
    Child 1 → Limit order (take profit)  ─┐ OCA group: cancel other on fill
    Child 2 → Stop order  (stop loss)    ─┘

    Callbacks:
        on_trade_closed(position, exit_price, exit_reason) → called on fill/cancel
    """

    def __init__(self, cfg: AppConfig, ib: IB):
        self.cfg = cfg
        self.ib = ib
        self._position: Optional[OpenPosition] = None
        self._lock = threading.Lock()
        self._on_trade_closed: Optional[Callable] = None
        self._bar_index: int = 0

        # Wire up IBKR fill events
        self.ib.execDetailsEvent += self._on_exec_details
        self.ib.orderStatusEvent += self._on_order_status

    def set_on_trade_closed(self, callback: Callable):
        """
        Register callback: fn(position, exit_price, exit_reason)
        Called when TP, SL, time stop, or manual close fills.
        """
        self._on_trade_closed = callback

    def update_bar_index(self, bar_index: int):
        self._bar_index = bar_index

    # ── Contract ──────────────────────────────────────────────────────────────

    def _spy_contract(self) -> Stock:
        ic = self.cfg.instrument
        return Stock(ic.symbol, ic.exchange, ic.currency)

    # ── Place bracket order ───────────────────────────────────────────────────

    def place_bracket_order(
        self,
        side: str,
        shares: int,
        entry_price: float,
        bar_index: int,
        bar_indicators: Dict,
    ) -> Optional[OpenPosition]:
        """
        Submit a bracket order (market entry + OCA TP/SL).
        Returns the OpenPosition if submitted, None on failure.
        """
        with self._lock:
            if self._position is not None:
                log.warning("place_bracket_order called but position already open. Ignoring.")
                return None

        exits = calculate_exit_prices(
            side,
            entry_price,
            self.cfg.exit.stop_loss_pct,
            self.cfg.exit.take_profit_pct,
        )
        tp_price = exits["tp"]
        sl_price = exits["sl"]

        action = "BUY" if side == LONG else "SELL"
        close_action = "SELL" if side == LONG else "BUY"

        contract = self._spy_contract()

        # ── Parent: Market order ───────────────────────────────────────────
        parent = Order()
        parent.action = action
        parent.orderType = "MKT"
        parent.totalQuantity = shares
        parent.transmit = False        # Hold — don't send until children ready
        parent.tif = "DAY"

        # ── Child 1: Take Profit (Limit) ───────────────────────────────────
        take_profit = Order()
        take_profit.action = close_action
        take_profit.orderType = "LMT"
        take_profit.totalQuantity = shares
        take_profit.lmtPrice = tp_price
        take_profit.tif = "DAY"
        take_profit.transmit = False

        # ── Child 2: Stop Loss (Stop) ──────────────────────────────────────
        stop_loss = Order()
        stop_loss.action = close_action
        stop_loss.orderType = "STP"
        stop_loss.totalQuantity = shares
        stop_loss.auxPrice = sl_price
        stop_loss.tif = "DAY"
        stop_loss.transmit = True       # Last order: transmits the whole group

        try:
            # Place via ib_insync bracket helper (handles parentId + OCA group)
            bracket = self.ib.bracketOrder(
                action,
                shares,
                limitPrice=tp_price,
                takeProfitPrice=tp_price,
                stopLossPrice=sl_price,
            )

            # Use ib_insync's bracket order (parent, takeProfit, stopLoss)
            parent_trade = self.ib.placeOrder(contract, bracket.parent)
            tp_trade     = self.ib.placeOrder(contract, bracket.takeProfit)
            sl_trade     = self.ib.placeOrder(contract, bracket.stopLoss)

            parent_id = bracket.parent.orderId
            tp_id     = bracket.takeProfit.orderId
            sl_id     = bracket.stopLoss.orderId

        except Exception as e:
            log.error(f"Failed to place bracket order: {e}")
            return None

        position = OpenPosition(
            side=side,
            entry_price=entry_price,
            shares=shares,
            entry_time=datetime.now(ET),
            bar_index=bar_index,
            tp_price=tp_price,
            sl_price=sl_price,
            parent_order_id=parent_id,
            tp_order_id=tp_id,
            sl_order_id=sl_id,
            entry_bar_indicators=bar_indicators,
        )

        with self._lock:
            self._position = position

        log.info(
            f"BRACKET ORDER PLACED | {side} {shares}sh @ ~${entry_price:.2f} | "
            f"TP: ${tp_price:.2f} | SL: ${sl_price:.2f} | "
            f"OrderIDs: parent={parent_id} tp={tp_id} sl={sl_id}"
        )
        return position

    # ── Time stop ─────────────────────────────────────────────────────────────

    def check_time_stop(self, current_bar_index: int) -> bool:
        """
        Check if the open position has exceeded the time stop (15 bars).
        If so, cancel TP/SL and flatten with a market order.
        Returns True if time stop was triggered.
        """
        with self._lock:
            pos = self._position

        if pos is None:
            return False

        bars_held = current_bar_index - pos.bar_index
        if bars_held < self.cfg.exit.time_stop_bars:
            return False

        log.info(
            f"TIME STOP triggered after {bars_held} bars. "
            f"Cancelling bracket and closing position."
        )
        self._cancel_child_orders(pos)
        self._flatten_position(pos, reason="TIME")
        return True

    def _cancel_child_orders(self, pos: OpenPosition):
        """Cancel the TP and SL child orders."""
        for order_id in [pos.tp_order_id, pos.sl_order_id]:
            try:
                # Find the trade by orderId
                for trade in self.ib.trades():
                    if trade.order.orderId == order_id:
                        self.ib.cancelOrder(trade.order)
                        log.debug(f"Cancelled order {order_id}")
                        break
            except Exception as e:
                log.warning(f"Error cancelling order {order_id}: {e}")

    def _flatten_position(self, pos: OpenPosition, reason: str):
        """Submit a market order to close the position immediately."""
        close_action = "SELL" if pos.side == LONG else "BUY"
        contract = self._spy_contract()

        close_order = Order()
        close_order.action = close_action
        close_order.orderType = "MKT"
        close_order.totalQuantity = pos.shares
        close_order.tif = "DAY"
        close_order.transmit = True

        try:
            trade = self.ib.placeOrder(contract, close_order)
            log.info(f"Flatten order submitted ({reason}): {close_action} {pos.shares} MKT")
        except Exception as e:
            log.error(f"Failed to flatten position: {e}")

    # ── Fill event handlers ───────────────────────────────────────────────────

    def _on_exec_details(self, trade: Trade, fill: Fill):
        """
        Called by ib_insync when any order fills.
        Identify if it's the entry, TP, or SL fill.
        """
        order_id = fill.execution.orderId
        fill_price = fill.execution.price
        fill_time = fill.time

        with self._lock:
            pos = self._position

        if pos is None:
            return

        if order_id == pos.parent_order_id:
            # Entry filled
            with self._lock:
                if self._position:
                    self._position.filled_entry_price = fill_price
            log.info(f"ENTRY FILLED @ ${fill_price:.2f} (ordered @ ${pos.entry_price:.2f})")

        elif order_id == pos.tp_order_id:
            log.info(f"TAKE PROFIT FILLED @ ${fill_price:.2f}")
            self._close_position(pos, fill_price, "TP")

        elif order_id == pos.sl_order_id:
            log.info(f"STOP LOSS FILLED @ ${fill_price:.2f}")
            self._close_position(pos, fill_price, "SL")

    def _on_order_status(self, trade: Trade):
        """
        Called by ib_insync on order status changes.
        Handle time-stop market close fills here.
        """
        with self._lock:
            pos = self._position

        if pos is None:
            return

        # Detect fills on our flatten/market close order (orderId won't match bracket)
        if (
            trade.orderStatus.status == "Filled"
            and trade.order.orderId not in [pos.parent_order_id, pos.tp_order_id, pos.sl_order_id]
            and trade.order.action in ("SELL", "BUY")
            and trade.order.orderType == "MKT"
        ):
            fill_price = trade.orderStatus.avgFillPrice
            log.info(f"TIME STOP CLOSE FILLED @ ${fill_price:.2f}")
            self._close_position(pos, fill_price, "TIME")

    def _close_position(self, pos: OpenPosition, exit_price: float, reason: str):
        """
        Clear internal position state and fire the on_trade_closed callback.
        """
        with self._lock:
            if self._position is None:
                return  # Already closed (race condition guard)
            self._position = None

        if self._on_trade_closed:
            self._on_trade_closed(pos, exit_price, reason)

    # ── State accessors ───────────────────────────────────────────────────────

    @property
    def has_open_position(self) -> bool:
        with self._lock:
            return self._position is not None

    @property
    def open_position(self) -> Optional[OpenPosition]:
        with self._lock:
            return self._position

    def manual_close_all(self):
        """
        Emergency manual close. Cancels open bracket orders and flattens.
        Use from kill switch or shutdown.
        """
        with self._lock:
            pos = self._position

        if pos is None:
            log.info("No open position to close.")
            return

        log.warning("MANUAL CLOSE: Cancelling bracket and flattening position.")
        self._cancel_child_orders(pos)
        self._flatten_position(pos, reason="MANUAL")

    def cancel_all_open_orders(self):
        """Cancel all open orders — run on startup to clear orphaned orders."""
        try:
            open_trades = self.ib.openTrades()
            for t in open_trades:
                self.ib.cancelOrder(t.order)
            if open_trades:
                log.info(f"Cancelled {len(open_trades)} orphaned open orders from previous session.")
        except Exception as e:
            log.warning(f"Error clearing open orders: {e}")

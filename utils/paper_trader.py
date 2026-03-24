"""
Paper-trading engine.
Simulates market-order fills at the current best bid/ask, tracks position,
average entry price, realised/unrealised PnL, and records every transaction.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class TradeRecord:
    timestamp: float          # unix ms
    strategy: str
    side: str                 # "buy" | "sell"
    price: float
    size: float
    position_after: float
    realized_pnl_delta: float # PnL realised on this fill
    cumulative_pnl: float     # running realised total


class PaperTrader:
    def __init__(self, strategy_name: str):
        self.strategy_name = strategy_name
        self.position: float = 0.0
        self.avg_entry: float = 0.0
        self.realized_pnl: float = 0.0
        self.num_trades: int = 0
        self.trades: list[TradeRecord] = []

    # ------------------------------------------------------------------
    def buy(self, price: float, size: float, timestamp: float = 0.0):
        if size <= 0:
            return
        ts = timestamp or time.time() * 1000
        rpnl = 0.0
        remaining = size

        # close short portion first
        if self.position < 0:
            close_qty = min(remaining, abs(self.position))
            rpnl = close_qty * (self.avg_entry - price)
            self.realized_pnl += rpnl
            self.position += close_qty
            remaining -= close_qty
            if abs(self.position) < 1e-12:
                self.position = 0.0
                self.avg_entry = 0.0

        # open / add-to long
        if remaining > 0:
            if self.position > 0:
                total_cost = self.avg_entry * self.position + price * remaining
                self.position += remaining
                self.avg_entry = total_cost / self.position
            else:
                self.position = remaining
                self.avg_entry = price

        self.num_trades += 1
        self.trades.append(TradeRecord(
            timestamp=ts, strategy=self.strategy_name, side="buy",
            price=price, size=size, position_after=self.position,
            realized_pnl_delta=rpnl, cumulative_pnl=self.realized_pnl,
        ))

    # ------------------------------------------------------------------
    def sell(self, price: float, size: float, timestamp: float = 0.0):
        if size <= 0:
            return
        ts = timestamp or time.time() * 1000
        rpnl = 0.0
        remaining = size

        # close long portion first
        if self.position > 0:
            close_qty = min(remaining, self.position)
            rpnl = close_qty * (price - self.avg_entry)
            self.realized_pnl += rpnl
            self.position -= close_qty
            remaining -= close_qty
            if abs(self.position) < 1e-12:
                self.position = 0.0
                self.avg_entry = 0.0

        # open / add-to short
        if remaining > 0:
            if self.position < 0:
                total_cost = self.avg_entry * abs(self.position) + price * remaining
                self.position -= remaining
                self.avg_entry = total_cost / abs(self.position)
            else:
                self.position = -remaining
                self.avg_entry = price

        self.num_trades += 1
        self.trades.append(TradeRecord(
            timestamp=ts, strategy=self.strategy_name, side="sell",
            price=price, size=size, position_after=self.position,
            realized_pnl_delta=rpnl, cumulative_pnl=self.realized_pnl,
        ))

    # ------------------------------------------------------------------
    def unrealized_pnl(self, mid_price: float) -> float:
        if self.position == 0 or mid_price == 0:
            return 0.0
        if self.position > 0:
            return self.position * (mid_price - self.avg_entry)
        return abs(self.position) * (self.avg_entry - mid_price)

    def total_pnl(self, mid_price: float) -> float:
        return self.realized_pnl + self.unrealized_pnl(mid_price)

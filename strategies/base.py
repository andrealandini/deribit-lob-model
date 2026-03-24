"""
Base class for paper-trading strategies.

Subclasses implement `on_book_update` and `on_trades` to compute a signal
in [-1, +1].  The base class converts that signal into a target position
via `_execute_signal`.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

from utils.paper_trader import PaperTrader


class BaseStrategy(ABC):
    def __init__(self, name: str, max_position: float = 0.01,
                 cooldown_ms: float = 5_000):
        self.name = name
        self.trader = PaperTrader(name)
        self.max_position = max_position
        self.cooldown_ms = cooldown_ms
        self.signal: float = 0.0
        self.mid_price: float = 0.0
        self._last_trade_ts: float = 0.0

    @abstractmethod
    def on_book_update(self, book: dict) -> None:
        """Called on every full order-book snapshot."""

    @abstractmethod
    def on_trades(self, trades: list[dict]) -> None:
        """Called on every batch of public trades."""

    def _execute_signal(self, best_bid: float, best_ask: float,
                        timestamp: float = 0.0):
        """Map current signal to a target position and trade the delta."""
        now = timestamp if timestamp else time.time() * 1000
        if now - self._last_trade_ts < self.cooldown_ms:
            return

        target = round(self.signal * self.max_position, 8)
        delta = target - self.trader.position

        if abs(delta) < 1e-8:
            return

        if delta > 0:
            self.trader.buy(best_ask, abs(delta), now)
        else:
            self.trader.sell(best_bid, abs(delta), now)

        self._last_trade_ts = now


class ReversedStrategy(BaseStrategy):
    """Wrapper that mirrors another strategy: negates its signal."""

    def __init__(self, inner: BaseStrategy):
        super().__init__(
            f"Rev {inner.name}",
            max_position=inner.max_position,
            cooldown_ms=inner.cooldown_ms,
        )
        self._inner = inner

    def on_book_update(self, book: dict) -> None:
        self.mid_price = self._inner.mid_price
        self.signal = -self._inner.signal
        bids, asks = book.get("bids", []), book.get("asks", [])
        if bids and asks:
            self._execute_signal(bids[0][0], asks[0][0],
                                 book.get("timestamp", 0))

    def on_trades(self, trades: list[dict]) -> None:
        pass  # inner already processed trades

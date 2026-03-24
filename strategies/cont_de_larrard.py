"""
Cont–de Larrard (2013) strategy.

Uses top-of-book queue imbalance to predict short-term price direction.
Imbalance = best_bid_qty / (best_bid_qty + best_ask_qty)
  > upper  ⇒  buy pressure  ⇒  long
  < lower  ⇒  sell pressure ⇒  short
"""

from strategies.base import BaseStrategy


class ContDeLarrard(BaseStrategy):
    def __init__(self, max_position: float = 0.01, cooldown_ms: float = 5_000,
                 upper: float = 0.60, lower: float = 0.40):
        super().__init__("Cont-de Larrard", max_position, cooldown_ms)
        self.upper = upper
        self.lower = lower
        self.imbalance: float = 0.5

    def on_book_update(self, book: dict) -> None:
        bids, asks = book["bids"], book["asks"]
        if not bids or not asks:
            return

        bid_qty = bids[0][1]
        ask_qty = asks[0][1]
        total = bid_qty + ask_qty
        if total == 0:
            return

        self.imbalance = bid_qty / total
        self.mid_price = (bids[0][0] + asks[0][0]) / 2

        if self.imbalance > self.upper:
            self.signal = (self.imbalance - self.upper) / (1.0 - self.upper)
        elif self.imbalance < self.lower:
            self.signal = (self.imbalance - self.lower) / self.lower
        else:
            self.signal = 0.0

        self._execute_signal(bids[0][0], asks[0][0],
                             book.get("timestamp", 0))

    def on_trades(self, trades: list[dict]) -> None:
        pass  # this strategy uses book data only

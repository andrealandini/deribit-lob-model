"""
Order Flow Imbalance (OFI) strategy – Cont, Kukanov & Stoikov (2014).

Tracks event-by-event changes at the best bid/ask to build a running
OFI measure.  Positive OFI ⇒ buy pressure ⇒ long; negative ⇒ short.
"""

from __future__ import annotations

from collections import deque

from strategies.base import BaseStrategy


class OFIStrategy(BaseStrategy):
    def __init__(self, max_position: float = 0.01, cooldown_ms: float = 5_000,
                 ofi_window: int = 50):
        super().__init__("OFI", max_position, cooldown_ms)
        self.ofi_window_size = ofi_window
        self.ofi_buf: deque[float] = deque(maxlen=ofi_window)

        self.prev_bid: float = 0.0
        self.prev_bid_sz: float = 0.0
        self.prev_ask: float = 0.0
        self.prev_ask_sz: float = 0.0

    def on_book_update(self, book: dict) -> None:
        bids, asks = book["bids"], book["asks"]
        if not bids or not asks:
            return

        bid, bid_sz = bids[0]
        ask, ask_sz = asks[0]
        self.mid_price = (bid + ask) / 2
        ts = book.get("timestamp", 0)

        # first tick: just store and return
        if self.prev_bid == 0:
            self.prev_bid, self.prev_bid_sz = bid, bid_sz
            self.prev_ask, self.prev_ask_sz = ask, ask_sz
            return

        # bid-side contribution
        e_b = 0.0
        if bid > self.prev_bid:
            e_b = bid_sz
        elif bid == self.prev_bid:
            e_b = bid_sz - self.prev_bid_sz
        else:
            e_b = -self.prev_bid_sz

        # ask-side contribution
        e_a = 0.0
        if ask < self.prev_ask:
            e_a = -ask_sz
        elif ask == self.prev_ask:
            e_a = -(ask_sz - self.prev_ask_sz)
        else:
            e_a = self.prev_ask_sz

        ofi = e_b + e_a
        self.ofi_buf.append(ofi)

        self.prev_bid, self.prev_bid_sz = bid, bid_sz
        self.prev_ask, self.prev_ask_sz = ask, ask_sz

        if len(self.ofi_buf) < 10:
            return

        cum_ofi = sum(self.ofi_buf)
        mean_sq = sum(x * x for x in self.ofi_buf) / len(self.ofi_buf)
        ofi_std = mean_sq ** 0.5
        if ofi_std > 0:
            z = cum_ofi / (ofi_std * len(self.ofi_buf) ** 0.5)
            self.signal = max(-1.0, min(1.0, z))
        else:
            self.signal = 0.0

        self._execute_signal(bid, ask, ts)

    def on_trades(self, trades: list[dict]) -> None:
        pass  # OFI uses book data only

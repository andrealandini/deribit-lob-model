"""
Kyle (1985) model strategy.

Estimates Kyle's lambda (price-impact coefficient) from the rolling
relationship  ΔPrice ≈ λ · ΔOrderFlow.  When recent net order-flow
implies a fair value above/below mid, the strategy goes long/short.
"""

from __future__ import annotations

from collections import deque

from strategies.base import BaseStrategy


class KyleStrategy(BaseStrategy):
    def __init__(self, max_position: float = 0.01, cooldown_ms: float = 5_000,
                 window: int = 200, warmup: int = 50,
                 flow_lookback: int = 20, sensitivity: float = 5_000):
        super().__init__("Kyle", max_position, cooldown_ms)
        self.window = window
        self.warmup = warmup
        self.flow_lookback = flow_lookback
        self.sensitivity = sensitivity

        self.net_flow: float = 0.0
        self.snapshots: deque[tuple[float, float, float]] = deque(maxlen=window)
        self.kyle_lambda: float = 0.0

    def on_trades(self, trades: list[dict]) -> None:
        for t in trades:
            signed = t["amount"] if t["direction"] == "buy" else -t["amount"]
            self.net_flow += signed

    def on_book_update(self, book: dict) -> None:
        bids, asks = book["bids"], book["asks"]
        if not bids or not asks:
            return

        self.mid_price = (bids[0][0] + asks[0][0]) / 2
        ts = book.get("timestamp", 0)
        self.snapshots.append((ts, self.net_flow, self.mid_price))

        if len(self.snapshots) < self.warmup:
            return

        # estimate λ = Cov(ΔP, ΔF) / Var(ΔF)
        n = len(self.snapshots)
        lookback = min(100, n - 1)
        dp, df = [], []
        for i in range(-lookback, 0):
            dp.append(self.snapshots[i][2] - self.snapshots[i - 1][2])
            df.append(self.snapshots[i][1] - self.snapshots[i - 1][1])

        var_f = sum(f * f for f in df)
        if var_f > 0:
            self.kyle_lambda = sum(p * f for p, f in zip(dp, df)) / var_f

        # predicted move from recent flow burst
        idx = max(0, n - self.flow_lookback)
        recent_flow = self.net_flow - self.snapshots[idx][1]
        predicted_move = self.kyle_lambda * recent_flow

        if self.mid_price > 0:
            rel_move = predicted_move / self.mid_price
            self.signal = max(-1.0, min(1.0, rel_move * self.sensitivity))

        self._execute_signal(bids[0][0], asks[0][0], ts)

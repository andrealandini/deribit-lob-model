"""
Ornstein-Uhlenbeck mean-reversion strategy.

Models the mid-price as a mean-reverting process:

    dX = θ (μ − X) dt + σ dW

Discrete-time form:  X_{t+1} = a + b·X_t + ε
    where  b = exp(−θ Δt),  a = μ (1 − b).

Online estimation via simple OLS regression of X_{t+1} on X_t.
Signal: z-score of the deviation from estimated long-run mean μ,
        so the strategy fades moves away from μ.
"""

from __future__ import annotations

import math
from collections import deque

from strategies.base import BaseStrategy


class OUMeanReversion(BaseStrategy):
    def __init__(self, max_position: float = 0.01, cooldown_ms: float = 5_000,
                 window: int = 400, warmup: int = 100,
                 z_entry: float = 1.5):
        super().__init__("OU MeanRev", max_position, cooldown_ms)
        self.window = window
        self.warmup = warmup
        self.z_entry = z_entry

        self.prices: deque[float] = deque(maxlen=window)

        # estimated parameters
        self.theta: float = 0.0   # mean-reversion speed
        self.mu_hat: float = 0.0  # long-run mean
        self.sigma_hat: float = 0.0
        self.z_score: float = 0.0

    def on_book_update(self, book: dict) -> None:
        bids, asks = book.get("bids", []), book.get("asks", [])
        if not bids or not asks:
            return

        mid = (bids[0][0] + asks[0][0]) / 2
        self.mid_price = mid
        ts = book.get("timestamp", 0)
        self.prices.append(mid)

        if len(self.prices) < self.warmup:
            return

        self._estimate()

        # fade the deviation: price above mu → short, below → long
        if self.sigma_hat > 0:
            self.z_score = (self.mu_hat - mid) / self.sigma_hat
            self.signal = max(-1.0, min(1.0, self.z_score / self.z_entry))
        else:
            self.signal = 0.0

        self._execute_signal(bids[0][0], asks[0][0], ts)

    def on_trades(self, trades: list[dict]) -> None:
        pass

    # ------------------------------------------------------------------
    def _estimate(self):
        """OLS:  X_{t+1} = a + b * X_t  ⟹  θ, μ, σ."""
        prices = list(self.prices)
        n = len(prices) - 1
        if n < 20:
            return

        # x = X_t, y = X_{t+1}
        sx = sy = sxx = sxy = 0.0
        for i in range(n):
            x, y = prices[i], prices[i + 1]
            sx += x
            sy += y
            sxx += x * x
            sxy += x * y

        denom = n * sxx - sx * sx
        if abs(denom) < 1e-20:
            return

        b = (n * sxy - sx * sy) / denom
        a = (sy - b * sx) / n

        # guard against b >= 1 (no mean reversion) or b <= 0
        if b <= 0 or b >= 1:
            self.signal = 0.0
            return

        self.theta = -math.log(b)            # mean-reversion speed
        self.mu_hat = a / (1.0 - b)          # long-run mean

        # residual std as proxy for σ
        ss_res = 0.0
        for i in range(n):
            resid = prices[i + 1] - (a + b * prices[i])
            ss_res += resid * resid
        self.sigma_hat = (ss_res / n) ** 0.5

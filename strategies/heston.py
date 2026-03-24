"""
Heston (1993) stochastic-volatility strategy.

Models price and variance as coupled processes:

    dS/S = μ dt + √v dW₁
    dv   = κ (θ − v) dt + ξ √v dW₂       (ρ = corr(dW₁, dW₂))

Online estimation:
  1. Compute rolling realised variance from log-returns.
  2. Estimate long-run variance θ̂ as the mean of the variance series.
  3. Build a momentum signal from recent returns.
  4. Scale the signal by vol regime:
       • realised vol < long-run  →  trending  →  amplify momentum
       • realised vol > long-run  →  choppy    →  dampen momentum
"""

from __future__ import annotations

import math
from collections import deque

from strategies.base import BaseStrategy

_RV_SPAN = 20        # ticks for short-term realised variance
_MOM_SPAN = 40       # ticks for momentum return


class HestonVol(BaseStrategy):
    def __init__(self, max_position: float = 0.01, cooldown_ms: float = 5_000,
                 window: int = 400, warmup: int = 80,
                 sensitivity: float = 600):
        super().__init__("Heston Vol", max_position, cooldown_ms)
        self.window = window
        self.warmup = warmup
        self.sensitivity = sensitivity

        self.prices: deque[float] = deque(maxlen=window)
        self.log_returns: deque[float] = deque(maxlen=window)
        self.rv_series: deque[float] = deque(maxlen=window)

        # estimated parameters
        self.rv_short: float = 0.0       # recent realised variance
        self.rv_long: float = 0.0        # long-run mean variance (θ)
        self.momentum: float = 0.0
        self.vol_ratio: float = 1.0      # θ / v  (> 1 = low-vol regime)

    def on_book_update(self, book: dict) -> None:
        bids, asks = book.get("bids", []), book.get("asks", [])
        if not bids or not asks:
            return

        mid = (bids[0][0] + asks[0][0]) / 2
        self.mid_price = mid
        ts = book.get("timestamp", 0)

        if self.prices:
            prev = self.prices[-1]
            if prev > 0:
                self.log_returns.append(math.log(mid / prev))

        self.prices.append(mid)

        if len(self.log_returns) < self.warmup:
            return

        self._estimate()
        self._execute_signal(bids[0][0], asks[0][0], ts)

    def on_trades(self, trades: list[dict]) -> None:
        pass

    # ------------------------------------------------------------------
    def _estimate(self):
        rets = list(self.log_returns)
        n = len(rets)

        # short-term realised variance
        span = min(_RV_SPAN, n)
        recent = rets[-span:]
        mean_r = sum(recent) / span
        self.rv_short = sum((r - mean_r) ** 2 for r in recent) / span

        # append to variance time-series & compute long-run mean
        self.rv_series.append(self.rv_short)
        if len(self.rv_series) > 1:
            self.rv_long = sum(self.rv_series) / len(self.rv_series)
        else:
            self.rv_long = self.rv_short

        # momentum: cumulative return over last MOM_SPAN ticks
        mom_span = min(_MOM_SPAN, n)
        self.momentum = sum(rets[-mom_span:])

        # vol-regime scaling
        if self.rv_short > 0:
            self.vol_ratio = self.rv_long / self.rv_short
        else:
            self.vol_ratio = 1.0

        # signal = momentum × vol_ratio  (amplify in quiet, dampen in noisy)
        raw = self.momentum * self.vol_ratio * self.sensitivity
        self.signal = max(-1.0, min(1.0, raw))

"""
Merton (1976) Jump-Diffusion strategy.

Models log-returns as a mixture of continuous Brownian drift and
discrete Poisson jumps:

    dS/S = μ dt + σ dW + J dN

where N is Poisson(λ) and J ~ N(μ_J, σ_J²).

Online estimation:
  1. Compute rolling log-returns from mid-price.
  2. Separate "normal" returns (|r| ≤ k·σ) from "jumps" (|r| > k·σ).
  3. Estimate  μ, σ  from normal returns  and  λ, μ_J, σ_J  from jumps.
  4. Expected return = μ + λ·μ_J.
  5. Signal ∝ expected return, clamped to [-1, +1].
"""

from __future__ import annotations

import math
from collections import deque

from strategies.base import BaseStrategy


class JumpDiffusion(BaseStrategy):
    def __init__(self, max_position: float = 0.01, cooldown_ms: float = 5_000,
                 window: int = 400, warmup: int = 60,
                 jump_threshold: float = 3.0, sensitivity: float = 800):
        super().__init__("Jump Diffusion", max_position, cooldown_ms)
        self.window = window
        self.warmup = warmup
        self.jump_threshold = jump_threshold
        self.sensitivity = sensitivity

        self.prices: deque[float] = deque(maxlen=window)
        self.log_returns: deque[float] = deque(maxlen=window)

        # estimated parameters
        self.mu: float = 0.0
        self.sigma: float = 0.0
        self.lam: float = 0.0      # jump intensity
        self.mu_j: float = 0.0     # mean jump size
        self.expected_return: float = 0.0

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
                lr = math.log(mid / prev)
                self.log_returns.append(lr)

        self.prices.append(mid)

        if len(self.log_returns) < self.warmup:
            return

        self._estimate()

        self.signal = max(-1.0, min(1.0,
                                    self.expected_return * self.sensitivity))
        self._execute_signal(bids[0][0], asks[0][0], ts)

    def on_trades(self, trades: list[dict]) -> None:
        pass

    # ------------------------------------------------------------------
    def _estimate(self):
        rets = list(self.log_returns)
        n = len(rets)

        # first-pass: full-sample mean & std
        mean_r = sum(rets) / n
        var_r = sum((r - mean_r) ** 2 for r in rets) / n
        std_r = var_r ** 0.5 if var_r > 0 else 1e-12

        # classify
        normal, jumps = [], []
        thresh = self.jump_threshold * std_r
        for r in rets:
            if abs(r - mean_r) > thresh:
                jumps.append(r)
            else:
                normal.append(r)

        # diffusion parameters from normal returns
        if normal:
            self.mu = sum(normal) / len(normal)
            var_n = sum((r - self.mu) ** 2 for r in normal) / len(normal)
            self.sigma = var_n ** 0.5
        else:
            self.mu = mean_r
            self.sigma = std_r

        # jump parameters
        if jumps:
            self.lam = len(jumps) / n
            self.mu_j = sum(jumps) / len(jumps)
        else:
            self.lam = 0.0
            self.mu_j = 0.0

        self.expected_return = self.mu + self.lam * self.mu_j

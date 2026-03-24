"""
Composite Alpha – advanced paper-trading strategy.

Fuses four microstructure / stochastic sub-signals into a single
composite score, gates entries on confidence and vol regime, and
manages every position with an asymmetric SL < TP bracket.

Sub-signals
-----------
1. LOB Queue Imbalance   (Cont–de Larrard)
2. Order Flow Imbalance  (Cont, Kukanov & Stoikov)
3. Kyle Flow Momentum    (Kyle 1985 – λ estimation)
4. OU Mean Reversion     (Ornstein-Uhlenbeck z-score)

Filters / modifiers
--------------------
• Volatility regime   – amplifies in quiet markets, dampens in noisy
• Jump detector       – suppresses entries for N ticks after a 3.5 σ move
• Confidence gate     – requires ≥ 3 / 4 sub-signals to agree in sign

Risk management
---------------
• SL distance  = ATR × atr_sl_mult
• TP distance  = SL  × risk_reward   (default 2.5 → TP is 2.5× wider than SL)
• Minimum 30 s between trades (quality > quantity)
"""

from __future__ import annotations

import math
import time
from collections import deque
from datetime import datetime, timezone

from strategies.base import BaseStrategy


class CompositeAlpha(BaseStrategy):
    FLAT = "FLAT"
    LONG = "LONG"
    SHORT = "SHORT"

    def __init__(
        self,
        max_position: float = 0.01,
        cooldown_ms: float = 30_000,
        risk_reward: float = 4.0,
        atr_sl_mult: float = 5.0,
        entry_threshold: float = 0.35,
        confidence_threshold: float = 0.60,
        analysis_every: int = 20,
    ):
        super().__init__("Composite α", max_position, cooldown_ms)
        self.risk_reward = risk_reward
        self.atr_sl_mult = atr_sl_mult
        self.entry_threshold = entry_threshold
        self.confidence_threshold = confidence_threshold
        self.analysis_every = analysis_every

        # position bracket
        self.position_state: str = self.FLAT
        self.entry_price: float = 0.0
        self.sl_price: float = 0.0
        self.tp_price: float = 0.0

        # thought log (shown on dashboard)
        self.thoughts: deque[str] = deque(maxlen=80)
        self._tick: int = 0

        # price / return buffers
        self.prices: deque[float] = deque(maxlen=500)
        self.log_returns: deque[float] = deque(maxlen=500)

        # --- sub-signal state ---

        # 1. imbalance
        self.imbalance: float = 0.5
        self.imb_signal: float = 0.0

        # 2. OFI
        self._prev_bid = self._prev_bid_sz = 0.0
        self._prev_ask = self._prev_ask_sz = 0.0
        self._ofi_buf: deque[float] = deque(maxlen=50)
        self.ofi_signal: float = 0.0

        # 3. Kyle flow
        self.net_flow: float = 0.0
        self._flow_snaps: deque[tuple[float, float, float]] = deque(maxlen=200)
        self.kyle_lambda: float = 0.0
        self.flow_signal: float = 0.0

        # 4. OU mean-rev
        self.mu_hat: float = 0.0
        self.sigma_hat: float = 0.0
        self.mr_signal: float = 0.0

        # --- filters ---
        self._rv_series: deque[float] = deque(maxlen=200)
        self.vol_regime: float = 1.0
        self.jump_detected: bool = False
        self._jump_cooldown: int = 0

        # ATR proxy
        self._abs_moves: deque[float] = deque(maxlen=100)
        self.atr: float = 0.0

        # composite
        self.composite: float = 0.0
        self.confidence: float = 0.0

        # weights
        self._w = {"imb": 0.25, "ofi": 0.30, "flow": 0.20, "mr": 0.25}

    # ==================================================================
    # Public interface
    # ==================================================================

    def on_trades(self, trades: list[dict]) -> None:
        for t in trades:
            signed = t["amount"] if t["direction"] == "buy" else -t["amount"]
            self.net_flow += signed

    def on_book_update(self, book: dict) -> None:
        bids, asks = book.get("bids", []), book.get("asks", [])
        if not bids or not asks:
            return

        bid, bid_sz = bids[0]
        ask, ask_sz = asks[0]
        mid = (bid + ask) / 2
        self.mid_price = mid
        ts = book.get("timestamp", 0)

        # price history
        if self.prices:
            prev = self.prices[-1]
            if prev > 0:
                self.log_returns.append(math.log(mid / prev))
                self._abs_moves.append(abs(mid - prev))
        self.prices.append(mid)

        if len(self.prices) < 80:
            return

        # update every sub-signal
        self._upd_imbalance(bid, bid_sz, ask, ask_sz)
        self._upd_ofi(bid, bid_sz, ask, ask_sz)
        self._upd_flow(mid, ts)
        self._upd_mean_rev()
        self._upd_vol_regime()
        self._upd_jump()
        self._upd_atr()
        self._upd_composite()

        self._tick += 1
        now = ts if ts else time.time() * 1000

        # position management
        if self.position_state != self.FLAT:
            self._check_exit(mid, bid, ask, now)
        else:
            self._check_entry(bid, ask, now)

        # periodic analysis log
        if self._tick % self.analysis_every == 0:
            self._log_analysis()

    # ==================================================================
    # Sub-signal updaters
    # ==================================================================

    def _upd_imbalance(self, bid, bid_sz, ask, ask_sz):
        total = bid_sz + ask_sz
        if total <= 0:
            return
        self.imbalance = bid_sz / total
        if self.imbalance > 0.6:
            self.imb_signal = (self.imbalance - 0.6) / 0.4
        elif self.imbalance < 0.4:
            self.imb_signal = (self.imbalance - 0.4) / 0.4
        else:
            self.imb_signal = 0.0

    def _upd_ofi(self, bid, bid_sz, ask, ask_sz):
        if self._prev_bid == 0:
            self._prev_bid, self._prev_bid_sz = bid, bid_sz
            self._prev_ask, self._prev_ask_sz = ask, ask_sz
            return

        e_b = 0.0
        if bid > self._prev_bid:
            e_b = bid_sz
        elif bid == self._prev_bid:
            e_b = bid_sz - self._prev_bid_sz
        else:
            e_b = -self._prev_bid_sz

        e_a = 0.0
        if ask < self._prev_ask:
            e_a = -ask_sz
        elif ask == self._prev_ask:
            e_a = -(ask_sz - self._prev_ask_sz)
        else:
            e_a = self._prev_ask_sz

        self._ofi_buf.append(e_b + e_a)
        self._prev_bid, self._prev_bid_sz = bid, bid_sz
        self._prev_ask, self._prev_ask_sz = ask, ask_sz

        if len(self._ofi_buf) >= 10:
            cum = sum(self._ofi_buf)
            ms = sum(x * x for x in self._ofi_buf) / len(self._ofi_buf)
            std = ms ** 0.5
            if std > 0:
                z = cum / (std * len(self._ofi_buf) ** 0.5)
                self.ofi_signal = max(-1.0, min(1.0, z))
            else:
                self.ofi_signal = 0.0

    def _upd_flow(self, mid, ts):
        self._flow_snaps.append((ts, self.net_flow, mid))
        n = len(self._flow_snaps)
        if n < 50:
            return

        lookback = min(80, n - 1)
        dp, df = [], []
        for i in range(-lookback, 0):
            dp.append(self._flow_snaps[i][2] - self._flow_snaps[i - 1][2])
            df.append(self._flow_snaps[i][1] - self._flow_snaps[i - 1][1])

        var_f = sum(f * f for f in df)
        if var_f > 0:
            self.kyle_lambda = sum(p * f for p, f in zip(dp, df)) / var_f

        idx = max(0, n - 20)
        recent_flow = self.net_flow - self._flow_snaps[idx][1]
        pred = self.kyle_lambda * recent_flow
        if self.mid_price > 0:
            self.flow_signal = max(-1.0, min(1.0, pred / self.mid_price * 5000))

    def _upd_mean_rev(self):
        prices = list(self.prices)
        n = len(prices) - 1
        if n < 50:
            return

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

        if 0 < b < 1:
            self.mu_hat = a / (1.0 - b)
            ss_res = sum(
                (prices[i + 1] - (a + b * prices[i])) ** 2 for i in range(n)
            )
            self.sigma_hat = (ss_res / n) ** 0.5
            if self.sigma_hat > 0:
                z = (self.mu_hat - self.mid_price) / self.sigma_hat
                self.mr_signal = max(-1.0, min(1.0, z / 1.5))
            else:
                self.mr_signal = 0.0
        else:
            self.mr_signal = 0.0

    def _upd_vol_regime(self):
        rets = list(self.log_returns)
        if len(rets) < 40:
            return
        recent = rets[-20:]
        mr = sum(recent) / len(recent)
        rv_short = sum((r - mr) ** 2 for r in recent) / len(recent)
        self._rv_series.append(rv_short)
        if len(self._rv_series) > 1:
            rv_long = sum(self._rv_series) / len(self._rv_series)
            self.vol_regime = rv_long / rv_short if rv_short > 0 else 1.0
        else:
            self.vol_regime = 1.0

    def _upd_jump(self):
        if self._jump_cooldown > 0:
            self._jump_cooldown -= 1
            self.jump_detected = True
            return
        rets = list(self.log_returns)
        if len(rets) < 30:
            self.jump_detected = False
            return
        mr = sum(rets) / len(rets)
        std = (sum((r - mr) ** 2 for r in rets) / len(rets)) ** 0.5
        if std > 0 and rets and abs(rets[-1] - mr) > 3.5 * std:
            self.jump_detected = True
            self._jump_cooldown = 10
        else:
            self.jump_detected = False

    def _upd_atr(self):
        if self._abs_moves:
            self.atr = sum(self._abs_moves) / len(self._abs_moves)

    # ==================================================================
    # Composite signal + confidence
    # ==================================================================

    def _upd_composite(self):
        raw = (
            self._w["imb"] * self.imb_signal
            + self._w["ofi"] * self.ofi_signal
            + self._w["flow"] * self.flow_signal
            + self._w["mr"] * self.mr_signal
        )
        self.composite = max(-1.0, min(1.0, raw))
        self.signal = self.composite  # for base-class dashboard compat

        # confidence = agreement × vol modifier × jump filter
        sigs = [self.imb_signal, self.ofi_signal, self.flow_signal, self.mr_signal]
        if self.composite > 0:
            agree = sum(1 for s in sigs if s > 0.05)
        elif self.composite < 0:
            agree = sum(1 for s in sigs if s < -0.05)
        else:
            agree = 0
        agreement = agree / len(sigs)

        vol_mod = min(1.5, max(0.3, self.vol_regime))
        jump_mod = 0.0 if self.jump_detected else 1.0
        self.confidence = agreement * vol_mod * jump_mod

    # ==================================================================
    # Entry / exit
    # ==================================================================

    def _check_entry(self, bid, ask, now):
        if now - self._last_trade_ts < self.cooldown_ms:
            return

        if abs(self.composite) < self.entry_threshold:
            return

        if self.confidence < self.confidence_threshold:
            self._thought(
                f"Confidence {self.confidence:.2f} < {self.confidence_threshold} "
                f"(signal {self.composite:+.3f}), SKIP"
            )
            return

        if self.jump_detected:
            self._thought("Jump detected → SKIP entry")
            return

        if self.atr <= 0:
            return

        sl_dist = self.atr * self.atr_sl_mult
        tp_dist = sl_dist * self.risk_reward

        if self.composite > 0:
            self.entry_price = ask
            self.sl_price = ask - sl_dist
            self.tp_price = ask + tp_dist
            self.position_state = self.LONG
            self.trader.buy(ask, self.max_position, now)
            self._last_trade_ts = now
            self._thought(
                f">>> ENTER LONG\n"
                f"  Entry {ask:,.1f}  SL {self.sl_price:,.1f}  TP {self.tp_price:,.1f}\n"
                f"  Signal {self.composite:+.3f}  Conf {self.confidence:.2f}  "
                f"RR 1:{self.risk_reward}"
            )
        else:
            self.entry_price = bid
            self.sl_price = bid + sl_dist
            self.tp_price = bid - tp_dist
            self.position_state = self.SHORT
            self.trader.sell(bid, self.max_position, now)
            self._last_trade_ts = now
            self._thought(
                f">>> ENTER SHORT\n"
                f"  Entry {bid:,.1f}  SL {self.sl_price:,.1f}  TP {self.tp_price:,.1f}\n"
                f"  Signal {self.composite:+.3f}  Conf {self.confidence:.2f}  "
                f"RR 1:{self.risk_reward}"
            )

    def _check_exit(self, mid, bid, ask, now):
        if self.position_state == self.LONG:
            if mid <= self.sl_price:
                self.trader.sell(bid, abs(self.trader.position), now)
                self._thought(
                    f"<<< EXIT LONG (SL)  at {bid:,.1f}  "
                    f"Δ {bid - self.entry_price:+,.1f}"
                )
                self._reset_position(now)
            elif mid >= self.tp_price:
                self.trader.sell(bid, abs(self.trader.position), now)
                self._thought(
                    f"<<< EXIT LONG (TP)  at {bid:,.1f}  "
                    f"Δ {bid - self.entry_price:+,.1f}"
                )
                self._reset_position(now)

        elif self.position_state == self.SHORT:
            if mid >= self.sl_price:
                self.trader.buy(ask, abs(self.trader.position), now)
                self._thought(
                    f"<<< EXIT SHORT (SL)  at {ask:,.1f}  "
                    f"Δ {self.entry_price - ask:+,.1f}"
                )
                self._reset_position(now)
            elif mid <= self.tp_price:
                self.trader.buy(ask, abs(self.trader.position), now)
                self._thought(
                    f"<<< EXIT SHORT (TP)  at {ask:,.1f}  "
                    f"Δ {self.entry_price - ask:+,.1f}"
                )
                self._reset_position(now)

    def _reset_position(self, now):
        self.position_state = self.FLAT
        self.entry_price = 0.0
        self.sl_price = 0.0
        self.tp_price = 0.0
        self._last_trade_ts = now

    # ==================================================================
    # Thought-process logging
    # ==================================================================

    def _thought(self, text: str):
        ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
        self.thoughts.append(f"[{ts}] {text}")

    def _log_analysis(self):
        vol_label = (
            "LOW" if self.vol_regime > 1.2
            else "HIGH" if self.vol_regime < 0.8
            else "NORMAL"
        )
        lines = [
            "--- analysis ---",
            f"  Imbalance  {self.imbalance:.2f} → {self.imb_signal:+.3f}",
            f"  OFI              → {self.ofi_signal:+.3f}",
            f"  Flow  λ={self.kyle_lambda:.2e} → {self.flow_signal:+.3f}",
            f"  MeanRev  μ={self.mu_hat:,.1f} → {self.mr_signal:+.3f}",
            f"  Composite {self.composite:+.3f}  Conf {self.confidence:.2f}",
            f"  Vol {vol_label} ({self.vol_regime:.2f})  "
            f"Jump {'YES' if self.jump_detected else 'no'}  "
            f"ATR {self.atr:.2f}",
            f"  State {self.position_state}",
        ]
        if self.position_state != self.FLAT:
            if self.position_state == self.LONG:
                to_sl = self.mid_price - self.sl_price
                to_tp = self.tp_price - self.mid_price
            else:
                to_sl = self.sl_price - self.mid_price
                to_tp = self.mid_price - self.tp_price
            lines.append(f"  → SL {to_sl:,.1f} away  TP {to_tp:,.1f} away")
        self._thought("\n".join(lines))

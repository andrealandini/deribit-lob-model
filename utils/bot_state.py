# bot_state.py
import asyncio
import uuid
import json
from utils.config import INSTRUMENT, ORDER_SIZE

from collections import deque
import time
import numpy as np


class BotState:
    def __init__(self):
        # --- shared state
        self.book_snapshot = {"bids": [], "asks": [], "incr_bids": [], "incr_asks": []}
        self.recent_trades = []
        self.current_position = 0.0
        self.average_price = 0.0
        self.step_counter = 0

        # --- events (await in strategy / future NN)
        self.new_snapshot_event = asyncio.Event()   # full book snapshot ready
        self.incr_book_event = asyncio.Event()      # incremental book update ready
        self.trades_event = asyncio.Event()         # new trades batch ready
        self.orders_event = asyncio.Event()         # order update occurred
        self.position_ready = asyncio.Event()       # private/get_position finished

        # --- rolling histories (ts, price) for quick features
        self.best_bid_history = deque(maxlen=1200)
        self.best_ask_history = deque(maxlen=1200)

        # --- active order tracking (cap at ≤1 per side)
        self.active_buy_label = None
        self.active_sell_label = None
        self.active_buy_price = None
        self.active_sell_price = None

    # ---------- updaters ----------
    def update_snapshot(self, snapshot: dict):
        self.book_snapshot["bids"] = snapshot.get("bids", [])
        self.book_snapshot["asks"] = snapshot.get("asks", [])

        bids = self.book_snapshot["bids"]
        asks = self.book_snapshot["asks"]
        if bids and asks:
            t = time.time()
            self.best_bid_history.append((t, bids[0][0]))  # price
            self.best_ask_history.append((t, asks[0][0]))  # price

        self.new_snapshot_event.set()

    def update_incr_book(self, incr_bids, incr_asks):
        self.book_snapshot["incr_bids"] = incr_bids or []
        self.book_snapshot["incr_asks"] = incr_asks or []
        self.incr_book_event.set()

    def update_trades(self, trades):
        self.recent_trades = trades or []
        self.trades_event.set()

    def notify_orders_event(self):
        self.orders_event.set()

    # ---------- order helpers ----------
    def generate_label(self, side: str) -> str:
        return f"{side}_{uuid.uuid4().hex[:8]}"

    def _clear_side(self, side: str):
        """Local bookkeeping: clear label & price for a side."""
        if side == "buy":
            self.active_buy_label = None
            self.active_buy_price = None
        else:
            self.active_sell_label = None
            self.active_sell_price = None

    async def cancel_order(self, ws, label: str):
        """Cancel by label; also clears local trackers if it matches the active one."""
        if not label:
            return

        # Optimistically clear local trackers for that side
        side = None
        if label == self.active_buy_label:
            side = "buy"
        elif label == self.active_sell_label:
            side = "sell"

        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 1001,
            "method": "private/cancel_by_label",
            "params": {"label": label}
        }))
        print(f"[CANCEL] Sent cancel for label {label}")

        if side:
            self._clear_side(side)

    async def place_order(self, ws, side: str, price: float):
        """
        Place a single limit order. Enforces ≤1 active per side:
        - If an order on that side already exists at the *same* price -> do nothing.
        - If it exists at a *different* price -> DO NOT place a new one here.
          Let strategy cancel first; place next tick (prevents duplicate bursts).
        """
        # Enforce 1-per-side invariant
        if side == "buy" and self.active_buy_label is not None:
            if self.active_buy_price == float(price):
                # already have a buy at this price -> noop
                print(f"[ORDER] Buy @ {price} exists (label={self.active_buy_label}), skip")
                return
            else:
                print(f"[ORDER] Buy exists at {self.active_buy_price}, won't place new @ {price} until cancel clears")
                return

        if side == "sell" and self.active_sell_label is not None:
            if self.active_sell_price == float(price):
                print(f"[ORDER] Sell @ {price} exists (label={self.active_sell_label}), skip")
                return
            else:
                print(f"[ORDER] Sell exists at {self.active_sell_price}, won't place new @ {price} until cancel clears")
                return

        label = self.generate_label(side)
        order = {
            "jsonrpc": "2.0",
            "id": 1000,
            "method": "private/buy" if side == "buy" else "private/sell",
            "params": {
                "instrument_name": INSTRUMENT,
                "amount": ORDER_SIZE,
                "type": "limit",
                "price": price,
                "post_only": True,
                "label": label
            }
        }
        await ws.send(json.dumps(order))
        print(f"[ORDER] Placed {side} @ {price} | Label: {label}")

        # Track active order per side
        if side == "buy":
            self.active_buy_label = label
            self.active_buy_price = float(price)
        else:
            self.active_sell_label = label
            self.active_sell_price = float(price)


    async def place_market_order(self, ws, side):
        label = self.generate_label(side)
        order = {
            "jsonrpc": "2.0",
            "id": 1000,
            "method": "private/buy" if side == "buy" else "private/sell",
            "params": {
                "instrument_name": INSTRUMENT,
                "amount": ORDER_SIZE,
                "type": "market",
                "label": label
            }
        }
        await ws.send(json.dumps(order))
        print(f"[MARKET ORDER] {side.upper()} {ORDER_SIZE} @ MARKET | Label: {label}")

        if side == "buy":
            self.active_buy_label = label
        else:
            self.active_sell_label = label


    # -------- Optional: single-call helper used by some strategies --------
    async def safe_place(self, ws, side: str, price: float):
        """
        Convenience: place if none; if price changed, cancel existing and return
        (so caller can wait for next tick before placing again). This is a
        simple pattern to cap outstanding orders to ≤1 per side safely.
        """
        if side == "buy":
            if self.active_buy_label is None:
                await self.place_order(ws, "buy", price)
            elif self.active_buy_price != float(price):
                await self.cancel_order(ws, self.active_buy_label)
                # placement deferred to next tick
        else:
            if self.active_sell_label is None:
                await self.place_order(ws, "sell", price)
            elif self.active_sell_price != float(price):
                await self.cancel_order(ws, self.active_sell_label)
                # placement deferred to next tick

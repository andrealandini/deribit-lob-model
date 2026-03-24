"""
run_model.py – Consumer coroutines that print order-book & trades
from BotState events.  Imported and launched by main.py.
"""

from datetime import datetime, timezone

from utils.bot_state import BotState
from utils.config import INSTRUMENT


def _ts(ms: int | float) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]


async def print_book(state: BotState):
    while True:
        await state.new_snapshot_event.wait()
        state.new_snapshot_event.clear()

        bids = state.book_snapshot["bids"][:5]
        asks = state.book_snapshot["asks"][:5]

        print(f"\n{'=' * 60}")
        print(f"  ORDER BOOK  {INSTRUMENT}  (top 5)")
        print(f"{'-' * 60}")
        print(f"  {'ASKS':>28}")
        for price, amount in reversed(asks):
            print(f"  {'':>16}{price:>10.1f}  {amount:>10.4f}")
        print(f"  {'---':>28}")
        for price, amount in bids:
            print(f"  {amount:<10.4f}  {price:<10.1f}")
        print(f"{'=' * 60}")


async def print_trades(state: BotState):
    while True:
        await state.trades_event.wait()
        state.trades_event.clear()

        for t in state.recent_trades:
            ts = _ts(t["timestamp"])
            direction = t["direction"].upper()
            price = t["price"]
            amount = t["amount"]
            arrow = "^" if direction == "BUY" else "v"
            print(f"  [{ts}] {arrow} {direction:<4}  {price:>10.1f}  x {amount}")

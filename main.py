"""
Deribit LOB Model – single entry point.

Streams live order-book & trades, runs three paper-trading strategies
(Cont-de Larrard, Kyle, OFI), shows a live comparison dashboard,
and exports all transactions to CSV on exit.
"""

import asyncio
import signal

from utils.bot_state import BotState
from utils.streams import stream_loop
from run_model import (
    build_strategies, book_feed, trade_feed,
    dashboard, export_transactions,
)


async def main():
    state = BotState()
    strategies = build_strategies()

    tasks = [
        asyncio.create_task(stream_loop(state), name="stream"),
        asyncio.create_task(book_feed(state, strategies), name="book_feed"),
        asyncio.create_task(trade_feed(state, strategies), name="trade_feed"),
        asyncio.create_task(dashboard(strategies), name="dashboard"),
    ]

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: [t.cancel() for t in tasks])

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        export_transactions(strategies)


if __name__ == "__main__":
    asyncio.run(main())

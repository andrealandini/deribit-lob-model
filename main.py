"""
Deribit – BTC order-book & trades streamer.

Single entry point: streams data into BotState and runs the model consumer.
"""

import asyncio
import signal

from utils.bot_state import BotState
from utils.streams import stream_loop
from run_model import print_book, print_trades


async def main():
    state = BotState()

    tasks = [
        asyncio.create_task(stream_loop(state), name="stream"),
        asyncio.create_task(print_book(state), name="print_book"),
        asyncio.create_task(print_trades(state), name="print_trades"),
    ]

    # On SIGINT / SIGTERM cancel all tasks for a clean exit
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: [t.cancel() for t in tasks])

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(main())

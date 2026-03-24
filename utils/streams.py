# utils/streams.py
"""
WebSocket streaming loop: connects to Deribit, authenticates (if credentials
are available), subscribes to order-book & trade channels, and feeds every
update into a shared BotState instance.
"""

import asyncio
import json
import ssl
import inspect
import random

import websockets
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError

from utils.config import DERIBIT_URI, INSTRUMENT, CLIENT_ID, CLIENT_SECRET
from utils.bot_state import BotState
from utils.message_handler import authenticate, subscribe, handle_messages


def _header_kwargs(headers):
    sig = inspect.signature(websockets.connect)
    params = sig.parameters
    if "additional_headers" in params:
        return {"additional_headers": headers}
    elif "extra_headers" in params:
        return {"extra_headers": headers}
    return {}


async def stream_loop(state: BotState):
    """Connect to Deribit and stream data into *state* with auto-reconnect."""
    backoff = 1
    ssl_ctx = ssl.create_default_context()
    origin = "https://test.deribit.com" if "test" in DERIBIT_URI else "https://www.deribit.com"
    headers = [("Origin", origin)]

    print(f"[INIT] Streaming {INSTRUMENT} from {DERIBIT_URI}")

    while True:
        try:
            kw = _header_kwargs(headers)
            async with websockets.connect(
                DERIBIT_URI, ssl=ssl_ctx,
                ping_interval=15, ping_timeout=10,
                close_timeout=3, max_queue=None,
                compression=None, **kw,
            ) as ws:
                print(f"[WS] Connected to {DERIBIT_URI}")

                # Enable server-side heartbeat
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "public/set_heartbeat",
                    "params": {"interval": 10}
                }))

                if CLIENT_ID and CLIENT_SECRET:
                    await authenticate(ws)

                await subscribe(ws)
                print(f"[WS] Subscribed to {INSTRUMENT} channels")

                backoff = 1
                await handle_messages(ws, state)

        except (ConnectionClosedOK, ConnectionClosedError, asyncio.TimeoutError, OSError) as e:
            print(f"[WS] Disconnected: {e} — reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(int(backoff * 2) + random.randint(0, 1), 60)
        except asyncio.CancelledError:
            print("[WS] Shutting down.")
            break
        except Exception as e:
            print(f"[WS][ERROR] {e} — reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(int(backoff * 2), 60)

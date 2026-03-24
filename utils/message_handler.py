# utils/message_handler.py

import json
import asyncio
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from utils.config import INSTRUMENT, CLIENT_ID, CLIENT_SECRET


async def authenticate(ws):
    await ws.send(json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "public/auth",
        "params": {
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET
        }
    }))


async def handle_messages(ws, state):
    """
    Reads messages from the websocket, updates shared BotState, and triggers events.
    Robust to connection drops: returns cleanly so the caller can reconnect.
    """
    last_filled = {}

    try:
        async for msg in ws:
            try:
                data = json.loads(msg)
            except Exception as e:
                print(f"[WS][PARSE] bad json: {e} | raw={msg[:160]!r}")
                continue

            # ---- Deribit heartbeat protocol ----
            # When you call public/set_heartbeat, Deribit sends:
            # {"method":"heartbeat","params":{"type":"test_request"}}
            # You MUST reply with public/test. Other heartbeat types can be ignored.
            if data.get("method") == "heartbeat":
                hb_type = (data.get("params") or {}).get("type")
                if hb_type == "test_request":
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": 8212,  # any client-chosen id
                        "method": "public/test",
                        "params": {}
                    }))
                    # print("[WS] replied to heartbeat test_request with public/test")
                else:
                    # could be 'heartbeat' / 'ping' / 'subscribe' → no action needed
                    pass
                continue

            # Some endpoints (rare/older examples) send method "test_request" directly.
            if data.get("method") == "test_request":
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": 8212,
                    "method": "public/test",
                    "params": {}
                }))
                continue
            # ---- end heartbeat handling ----

            # Ignore simple RPC acks (auth/subscription/etc.)
            if data.get("id") in (1, 2, 3):
                continue

            # Handle position snapshot
            if data.get("id") == 3000 and "result" in data:
                try:
                    res = data["result"]
                    state.current_position = res.get("size", 0.0)
                    state.average_price = res.get("average_price", 0.0)
                    state.position_ready.set()
                except Exception as e:
                    print(f"[WS][POS][ERR] {e} | payload={data}")
                continue

            # Streaming channels
            if data.get("method") == "subscription":
                try:
                    channel = data["params"]["channel"]
                    payload = data["params"]["data"]
                except Exception as e:
                    print(f"[WS][SUB][ERR] {e} | payload={data}")
                    continue

                # Full book snapshot (20 levels, 100ms)
                if channel == f"book.{INSTRUMENT}.none.20.100ms":
                    if isinstance(payload, dict) and "bids" in payload and "asks" in payload:
                        state.update_snapshot({
                            "bids": payload.get("bids", []),
                            "asks": payload.get("asks", []),
                            "timestamp": payload.get("timestamp", 0),
                        })

                # Incremental book updates
                elif channel == f"book.{INSTRUMENT}.100ms":
                    state.update_incr_book(
                        payload.get("bids", []) if isinstance(payload, dict) else [],
                        payload.get("asks", []) if isinstance(payload, dict) else []
                    )

                # Public trades
                elif channel == f"trades.{INSTRUMENT}.100ms":
                    if isinstance(payload, list):
                        state.update_trades(payload)

                # Private user orders
                elif channel.startswith("user.orders.") and isinstance(payload, list):
                    for order in payload:
                        try:
                            state_name = order.get("order_state", "")
                            order_id = order.get("order_id", "")
                            filled = order.get("filled_amount", 0.0)
                            if state_name in ("filled", "partially_filled") and last_filled.get(order_id) != filled:
                                last_filled[order_id] = filled
                                await request_position(ws)
                        except Exception as e:
                            print(f"[WS][ORD][ERR] {e} | order={order}")
                    state.notify_orders_event()

            # else:
            #     print(f"[WS][UNHANDLED] {data}")

    except (ConnectionClosedOK, ConnectionClosedError) as e:
        print(f"[WS] reader closed — {e}. Reconnect will be handled by caller.")
        return
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[WS][READ][ERR] {e}")
        return


async def subscribe(ws):
    # Public channels
    await ws.send(json.dumps({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "public/subscribe",
        "params": {
            "channels": [
                f"book.{INSTRUMENT}.none.20.100ms",
                f"book.{INSTRUMENT}.100ms",
                f"trades.{INSTRUMENT}.100ms",
            ]
        }
    }))
    # Private channels
    await ws.send(json.dumps({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "private/subscribe",
        "params": {
            "channels": [
                f"user.orders.{INSTRUMENT}.100ms"
            ]
        }
    }))


async def request_position(ws):
    await ws.send(json.dumps({
        "jsonrpc": "2.0",
        "id": 3000,
        "method": "private/get_position",
        "params": {"instrument_name": INSTRUMENT}
    }))
    
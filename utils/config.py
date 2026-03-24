import os
from dotenv import load_dotenv

load_dotenv()

DERIBIT_URI = os.getenv("DERIBIT_WS_URI", "wss://www.deribit.com/ws/api/v2")
CLIENT_ID = os.getenv("DERIBIT_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DERIBIT_CLIENT_SECRET", "")
INSTRUMENT = os.getenv("INSTRUMENT", "BTC_USDC")
ORDER_SIZE = float(os.getenv("ORDER_SIZE", "1.0"))

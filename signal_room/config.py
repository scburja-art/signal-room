"""Settings, all from environment variables. No secrets in code."""
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./signal_room.db")
MASTER_KEY = os.getenv("SIGNAL_ROOM_MASTER_KEY", "")
HOST = os.getenv("SIGNAL_ROOM_HOST", "127.0.0.1")
PORT = int(os.getenv("SIGNAL_ROOM_PORT", "8000"))

PAIRS = ("BTC", "ETH", "SOL", "XRP")
HORIZONS = {"15m": 15 * 60, "1h": 60 * 60, "4h": 4 * 60 * 60, "24h": 24 * 60 * 60}

MIN_CONFIDENCE = 0.50
MAX_CONFIDENCE = 1.00

SIGNATURE_WINDOW_SECONDS = 60
PRICE_MAX_AGE_SECONDS = 180          # a signal needs a reference price no older than this
RESOLVE_TOLERANCE_SECONDS = 90       # nearest snapshot to (submit + horizon) must be within this
FLAT_BAND = 0.0005                   # ±0.05% resolves as flat
ROUND_TRIP_COST = 0.001              # 0.10% assumed cost for net-return metric

STARTER_CREDITS = 10.0
READ_COST = 1.0
MIN_RESOLVED_FOR_RANK = 30
SHRINK_N = 30
DELIST_BSS = -0.10
DELIST_WINDOW = 90
REINSTATE_MIN_RESOLVED = 30   # fresh signals after delisting, with BSS > 0, to come back
RETIRE_AFTER_DELISTS = 3      # within 12 months

RATE_LIMIT_PER_MINUTE = 60
REGISTRATIONS_PER_HOUR = 20   # interim brake on free starter credits until membership gates registration
PRICE_SAMPLE_SECONDS = 60

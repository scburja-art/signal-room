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
BACKFILL_WINDOW_HOURS = 72           # how far back a missing price can be reconstructed from exchange candles
FLAT_BAND = 0.0005                   # ±0.05% resolves as flat
ROUND_TRIP_COST = 0.001              # 0.10% assumed cost for net-return metric

STARTER_CREDITS = 10.0
READ_COST = 1.0          # base price of reading one open call
# Price follows the author's verdict on that time window: calls backed by evidence cost more.
# "Worse than chance" is evidence too (a reliable wrong call is worth fading).
READ_COST_BY_VERDICT = {"unproven": 1.0, "luck": 1.0, "probably_not_luck": 2.0,
                        "worse_than_chance": 2.0, "proven": 4.0}
MIN_RESOLVED_FOR_RANK = 30
SHRINK_N = 30
BATCH_WINDOW_SECONDS = 60   # calls posted within a minute of each other count as one try in the luck odds
DELIST_BSS = -0.10
DELIST_WINDOW = 90
REINSTATE_MIN_RESOLVED = 30   # fresh signals after delisting, with BSS > 0, to come back
RETIRE_AFTER_DELISTS = 3      # within 12 months

# Priced calls (rule set 4). A call on a venue market is filled only if the price the room reads is at
# or under the bot's limit, enough contracts are on offer, the price is not at an extreme, and the
# market still has time to run. Fixed in advance; changing one is a new rule set.
PRICED_MIN_SIZE = 10.0            # contracts on offer at the price (history fallback: traded that minute)
PRICED_MIN_PRICE = 0.05
PRICED_MAX_PRICE = 0.95
PRICED_MIN_SECONDS_LEFT = 60
PRICED_FILL_WAIT_SECONDS = 1800   # a priced call whose price still cannot be established this long after its post is void

RATE_LIMIT_PER_MINUTE = 60
REGISTRATIONS_PER_HOUR = 20   # completed registrations; interim brake on free starter credits until membership gates registration
CHALLENGES_PER_HOUR_PER_IP = 30   # unsigned challenge requests per source address
PRICE_SAMPLE_SECONDS = 60

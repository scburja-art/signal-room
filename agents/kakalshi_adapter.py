"""Kakalshi adapter: turns Kakalshi's shadow bets into Signal Room signals.

Watches shadow_results.json. Each new crypto bet becomes one signal:
  ticker KXSOL15M-...  -> pair SOL, horizon 15m
  side yes             -> long (price above the strike at close)
  side no              -> short
  price_paid (cents)   -> confidence: the market-implied probability that the chosen side wins.
                          A yes at 33c means 33% implied for yes. A no at 33c means the no side
                          was bought at 33c, i.e. 33% implied for no. Either way the price paid is
                          the implied probability of the side taken, clamped to 0.50-0.95.

Kakalshi records carry confidence 0, so the market-implied probability of the side it took
is the best available stand-in. Entries below 50c mean Kakalshi bet against the market's odds;
those are posted at the floor of 0.50 (a 50/50 call) rather than dropped, so the record is complete.

Timestamps in the shadow log are UTC (Kalshi's clock); the adapter compares in UTC.

Only bets whose timestamp is after the adapter started are posted, so an old log does not
flood the room with stale signals. Posted bets are remembered by id + timestamp, because
Kakalshi restarts bet numbering at 1 whenever it resets its shadow log.

Each Kakalshi strategy gets its own Signal Room agent (own wallet, own record), so the room
scores them separately. The strategy name comes from the bet's `strategy` field (falling back
to `source`). Wallets are created on first sight and kept in the state directory as
`kakalshi-<strategy>.json`.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import time

from .client import SignalRoomClient, SignalRoomError

log = logging.getLogger("agents.kakalshi")

SERIES_PAIR = {"KXBTC": "BTC", "KXETH": "ETH", "KXSOL": "SOL", "KXXRP": "XRP"}
SERIES_HORIZON = {"15M": "15m", "": "1h"}  # KXBTC15M -> 15m; plain KXBTC hourly -> 1h
TICKER_RE = re.compile(r"^(KX(?:BTC|ETH|SOL|XRP))(15M)?-")
CONF_MIN, CONF_MAX = 0.50, 0.95


def parse_bet(bet: dict) -> tuple[str, str, str, float] | None:
    m = TICKER_RE.match(bet.get("ticker", ""))
    if not m:
        return None
    pair = SERIES_PAIR[m.group(1)]
    horizon = SERIES_HORIZON[m.group(2) or ""]
    side = bet.get("side")
    if side not in ("yes", "no"):
        return None
    direction = "long" if side == "yes" else "short"
    price_c = bet.get("price_paid")
    if price_c is None:
        return None
    confidence = min(CONF_MAX, max(CONF_MIN, float(price_c) / 100.0))
    return pair, horizon, direction, round(confidence, 4)


def load_bets(path: str) -> list[dict]:
    """Read the shadow log. Kakalshi may be mid-write, so a torn or empty file returns [] rather than raising."""
    try:
        with open(path) as f:
            raw = f.read()
    except FileNotFoundError:
        return []
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    bets = data.get("bets", data if isinstance(data, list) else [])
    return [b for b in bets if isinstance(b, dict)]


def strategy_of(bet: dict) -> str:
    name = str(bet.get("strategy") or bet.get("source") or "unknown")
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "unknown"


def main(base_url: str, shadow_path: str, state_path: str, poll_seconds: int = 20) -> None:
    state_dir = os.path.dirname(state_path) or "."
    clients: dict[str, SignalRoomClient] = {}

    def client_for(strategy: str) -> SignalRoomClient:
        if strategy not in clients:
            path = os.path.join(state_dir, f"kakalshi-{strategy}.json")
            clients[strategy] = SignalRoomClient.load_or_register(base_url, path, name=f"kakalshi-{strategy}")
            log.info("strategy %s -> agent %s", strategy, clients[strategy].agent_id)
        return clients[strategy]

    posted_path = state_path + ".posted.json"
    posted: set = set(json.load(open(posted_path))) if os.path.exists(posted_path) else set()
    started = dt.datetime.now(dt.timezone.utc)
    log.info("kakalshi adapter watching %s (one agent per strategy)", shadow_path)
    waiting_logged = False
    while True:
        try:
            bets = load_bets(shadow_path)
        except Exception as e:  # anything unexpected: log once per occurrence, keep going
            log.warning("could not read shadow log: %s", e)
            time.sleep(poll_seconds)
            continue
        if not bets and not os.path.exists(shadow_path):
            if not waiting_logged:
                log.info("waiting for %s to appear (Kakalshi creates it on its first bet)", shadow_path)
                waiting_logged = True
            time.sleep(poll_seconds)
            continue
        waiting_logged = False
        for bet in bets:
            bid = f"{bet.get('id')}@{bet.get('timestamp', '')}"
            if bid in posted:
                continue
            try:
                ts = dt.datetime.fromisoformat(bet["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
            except Exception:
                posted.add(bid)
                continue
            if ts < started:
                posted.add(bid)  # history, not a live prediction
                continue
            parsed = parse_bet(bet)
            if parsed is None:
                posted.add(bid)
                continue
            pair, horizon, direction, confidence = parsed
            strategy = strategy_of(bet)
            try:
                out = client_for(strategy).post_signal(pair, horizon, direction, confidence)
                log.info("bet %s [%s] -> %s %s %s conf=%.2f id=%s", bid, strategy, pair, horizon, direction, confidence, out["signal_id"])
                posted.add(bid)
            except SignalRoomError as e:
                if e.status == 409:
                    log.info("bet %s [%s]: open signal for %s %s already pending, will retry", bid, strategy, pair, horizon)
                else:
                    log.warning("bet %s: %s", bid, e)
                    posted.add(bid)
            except Exception as e:  # network blip, server restart: retry next poll
                log.warning("bet %s: post failed (%s), will retry", bid, type(e).__name__)
        with open(posted_path, "w") as f:
            json.dump(sorted(posted), f)
        time.sleep(poll_seconds)

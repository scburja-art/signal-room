"""Kakalshi adapter: turns Kakalshi's shadow bets into Signal Room signals.

Watches shadow_results.json. Each new crypto bet becomes one signal:
  ticker KXSOL15M-...  -> pair SOL, horizon 15m
  side yes             -> long (price above the strike at close)
  side no              -> short
  price_paid (cents)   -> confidence = price paid / 100, clamped to 0.50-0.95

Kakalshi records carry confidence 0, so the market-implied probability of the side it took
is the best available stand-in. Tune CONF_MIN / CONF_MAX or swap in a fixed value here.

Only bets whose timestamp is after the adapter started are posted, so an old log does not
flood the room with stale signals. Posted bet ids are kept in the state file.
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
    with open(path) as f:
        data = json.load(f)
    bets = data.get("bets", data if isinstance(data, list) else [])
    return [b for b in bets if isinstance(b, dict)]


def main(base_url: str, shadow_path: str, state_path: str, poll_seconds: int = 20) -> None:
    client = SignalRoomClient.load_or_register(base_url, state_path, name="kakalshi-shadow")
    posted_path = state_path + ".posted.json"
    posted: set = set(json.load(open(posted_path))) if os.path.exists(posted_path) else set()
    started = dt.datetime.now(dt.timezone.utc)
    log.info("kakalshi adapter running as %s, watching %s", client.agent_id, shadow_path)
    while True:
        try:
            bets = load_bets(shadow_path)
        except Exception as e:
            log.warning("could not read shadow log: %s", e)
            time.sleep(poll_seconds)
            continue
        for bet in bets:
            bid = str(bet.get("id"))
            if bid in posted:
                continue
            try:
                ts = dt.datetime.fromisoformat(bet["timestamp"])
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
            try:
                out = client.post_signal(pair, horizon, direction, confidence)
                log.info("bet %s -> %s %s %s conf=%.2f id=%s", bid, pair, horizon, direction, confidence, out["signal_id"])
                posted.add(bid)
            except SignalRoomError as e:
                if e.status == 409:
                    log.info("bet %s: open signal for %s %s already pending, will retry", bid, pair, horizon)
                else:
                    log.warning("bet %s: %s", bid, e)
                    posted.add(bid)
        with open(posted_path, "w") as f:
            json.dump(sorted(posted), f)
        time.sleep(poll_seconds)

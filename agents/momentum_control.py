"""Control agent: a dumb momentum rule, posted every hour.

Purpose: prove the scorer is honest. A rule this simple should land near zero skill.
Rule: for each pair, if the last completed hour's return was positive, call long; otherwise short.
Confidence is fixed at 0.55. Horizon 1h. Posts just after the top of each hour, after the
previous signal has had time to resolve.

Reads the reference price from the room's own /me history when available; otherwise
from Coinbase's public candles (deterministic, no keys).
"""
from __future__ import annotations

import logging
import time

import httpx

from .client import SignalRoomClient, SignalRoomError

log = logging.getLogger("agents.momentum")
PAIRS = ("BTC", "ETH", "SOL", "XRP")
CONFIDENCE = 0.55


def last_hour_return(http: httpx.Client, pair: str) -> float | None:
    r = http.get(f"https://api.exchange.coinbase.com/products/{pair}-USD/candles", params={"granularity": 3600}, timeout=10)
    r.raise_for_status()
    candles = r.json()  # [time, low, high, open, close, volume], newest first; [0] is the hour in progress
    if len(candles) < 3:
        return None
    last_complete, before_that = candles[1][4], candles[2][4]
    return (last_complete - before_that) / before_that


def run_once(client: SignalRoomClient, http: httpx.Client) -> None:
    for pair in PAIRS:
        try:
            ret = last_hour_return(http, pair)
        except Exception as e:
            log.warning("%s: candle fetch failed: %s", pair, e)
            continue
        if ret is None:
            continue
        direction = "long" if ret > 0 else "short"
        try:
            out = client.post_signal(pair, "1h", direction, CONFIDENCE)
            log.info("%s %s @%.2f -> %s", pair, direction, out["price_at_submit"], out["signal_id"])
        except SignalRoomError as e:
            if e.status == 409:
                log.info("%s: open signal still pending", pair)
            else:
                log.warning("%s: %s", pair, e)


POST_OFFSET_SECONDS = 150  # 2.5 minutes past the hour: the previous 1h signal has resolved by then


def seconds_until_next_slot(now: float | None = None) -> float:
    now = now if now is not None else time.time()
    next_hour = (int(now) // 3600 + 1) * 3600
    return max(1.0, next_hour + POST_OFFSET_SECONDS - now)


def main(base_url: str, state_path: str) -> None:
    client = SignalRoomClient.load_or_register(base_url, state_path, name="momentum-control")
    http = httpx.Client()
    log.info("momentum control running as %s", client.agent_id)
    run_once(client, http)
    while True:
        time.sleep(seconds_until_next_slot())
        run_once(client, http)

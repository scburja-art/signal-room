"""Control agent: a dumb momentum rule, posted every hour.

Purpose: prove the scorer is honest. A rule this simple should land near zero skill.
Rule: for each pair, if the last completed hour's return was positive, call long; otherwise short.
Confidence is fixed at 0.55. Horizon 1h. Posts again as soon as the previous
signal has been scored, so about once an hour.

Hourly candles come from Coinbase's public API (deterministic, no keys).
"""
from __future__ import annotations

import datetime as dt
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


def run_once(client: SignalRoomClient, http: httpx.Client) -> str | None:
    """Post one signal per pair. Returns the latest resolve_at (ISO) among the posts that went through."""
    latest = None
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
            latest = max(latest or "", out["resolve_at"])
        except SignalRoomError as e:
            if e.status == 409:
                log.info("%s: open signal still pending", pair)
            else:
                log.warning("%s: %s", pair, e)
    return latest


RESOLVE_LAG_SECONDS = 75   # the server scores a signal on the first minute tick after its horizon
RETRY_SECONDS = 30         # when nothing could be posted (previous still open, feeds down)


def seconds_until_next_post(latest_resolve_at: str | None, now: dt.datetime | None = None) -> float:
    """Post again as soon as the previous signal has been scored. A 1h signal posted on the hour is
    still open at the next hour mark (horizon == interval), so a fixed hourly slot skips every other
    hour; keying off the horizon instead costs about a minute of drift per cycle and misses nothing."""
    if latest_resolve_at is None:
        return RETRY_SECONDS
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    wait = (dt.datetime.fromisoformat(latest_resolve_at) - now).total_seconds() + RESOLVE_LAG_SECONDS
    return max(float(RETRY_SECONDS), wait)


def main(base_url: str, state_path: str) -> None:
    client = SignalRoomClient.load_or_register(base_url, state_path, name="momentum-control")
    http = httpx.Client()
    log.info("momentum control running as %s", client.agent_id)
    while True:
        time.sleep(seconds_until_next_post(run_once(client, http)))

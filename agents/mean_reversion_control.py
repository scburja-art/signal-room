"""Second control agent: mean reversion, the mirror image of the momentum control.

Rule: for each pair, if the last completed hour was up, call short (expect a pullback); if down,
call long. Confidence fixed at 0.55. Horizon 1h. Posts again as soon as the previous one is scored.

Together with momentum this brackets the scorer: two dumb rules with opposite theses. Over a long
run both should sit near zero skill. If one of them drifts clearly positive, either the market has
a persistent one-hour autocorrelation or the scorer has a bias, and either way it is worth knowing.
"""
from __future__ import annotations

import logging
import time

import httpx

from .client import SignalRoomClient, SignalRoomError
from .momentum_control import PAIRS, last_hour_return, seconds_until_next_post

log = logging.getLogger("agents.meanrev")
CONFIDENCE = 0.55


def run_once(client: SignalRoomClient, http: httpx.Client) -> str | None:
    latest = None
    for pair in PAIRS:
        try:
            ret = last_hour_return(http, pair)
        except Exception as e:
            log.warning("%s: candle fetch failed: %s", pair, e)
            continue
        if ret is None:
            continue
        direction = "short" if ret > 0 else "long"
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


def main(base_url: str, state_path: str) -> None:
    client = SignalRoomClient.load_or_register(base_url, state_path, name="meanrev-control")
    http = httpx.Client()
    log.info("mean-reversion control running as %s", client.agent_id)
    while True:
        time.sleep(seconds_until_next_post(run_once(client, http)))

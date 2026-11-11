"""Control agent: buys the favourite in every 15-minute window, and claims nothing.

Purpose: show what "no skill" looks like on a venue market. If Kalshi's favourites are simply
underpriced, a fair test would call any favourite-buyer skilled. This bot is the yardstick for that:
a real bot's priced record means something only as far as it beats this one.

Rule: 90 seconds into each UTC quarter-hour (the windows Kalshi's 15-minute markets use, and about
when a first-minute strategy acts), for each of the room's four coins, read Kalshi's public price for
both sides and buy whichever side costs more, for up to 2c over the price just read (the price can
tick in the moment between this bot's look and the room's, and a favourite-buyer would still buy;
the room never fills above 95c either way). No stated chance,
so the room scores each call as saying exactly what it paid: it can earn no credits and no skill,
only a verdict. A window where the two sides cost the same is skipped. A window the bot cannot act on
within a minute of its time (the machine slept, the bot was started mid-window) is missed, never
posted late: a favourite bought ten minutes in is a different, surer bet, and this bot is the
yardstick. Each window runs once.

Kalshi's prices are read from its public data (no keys). Settings are fixed in code; a change ships
under a new agent name and a fresh record.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

from signal_room import kalshi

from .client import SignalRoomClient, SignalRoomError

log = logging.getLogger("agents.favourite")
NAME = "favourite-control"
SERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M", "XRP": "KXXRP15M"}
WINDOW = 900        # seconds in a window
ACT_AT = 90         # seconds into the window
TOO_LATE = 150      # past this point in a window the slot is missed, not run late
SLACK = 0.02        # it will pay up to 2c over the price it just read, like a small market order would


def pick(market: dict) -> tuple[str, float] | None:
    """(side, price) of the favourite: the side that costs more. None when either side has nothing on
    offer or the two cost the same."""
    yes, no = kalshi.quote(market, "yes"), kalshi.quote(market, "no")
    if yes.ask is None or no.ask is None or yes.ask == no.ask:
        return None
    return ("yes", yes.ask) if yes.ask > no.ask else ("no", no.ask)


def next_window(now: float, done: int | None) -> int:
    """The next slot to run: this window's if it is still early enough and not yet run, else the next one."""
    w = int(now // WINDOW) * WINDOW
    if now > w + TOO_LATE or (done is not None and w <= done):
        w = max(w, done or w) + WINDOW
    return w


def run_slot(client: SignalRoomClient, w: int, http=None, now: float | None = None, clock=None) -> int:
    """One window: one priced call per coin on that window's market. Returns how many were accepted.
    The time is looked at before every coin (clock: a function giving unix seconds; now: a fixed time,
    for tests); past TOO_LATE in the window nothing more is posted, however slow the earlier posts were."""
    clock = clock or ((lambda: now) if now is not None else (lambda: w + ACT_AT))
    posted = 0
    for pair, series in SERIES.items():
        t = clock()
        if not (w <= t <= w + TOO_LATE):
            log.info("window %s: %.0f s in, too late to act as the yardstick; %s and any coins after it are missed",
                     time.strftime("%H:%M", time.gmtime(w)), t - w, pair)
            break
        at = dt.datetime.fromtimestamp(t, dt.timezone.utc).replace(tzinfo=None)
        try:
            market = kalshi.open_market(series, at, http)
            choice = pick(market) if market else None
        except kalshi.VenueError as e:
            log.warning("%s: could not read Kalshi (%s); no %s call this window", pair, e, pair)
            continue
        if choice is None:
            log.info("%s: no favourite to buy this window", pair)
            continue
        side, price = choice
        try:
            out = client.post_priced(market["ticker"], side, min(0.95, round(price + SLACK, 4)))
            log.info("%s %s posted -> %s (%s)", pair, side, out["signal_id"], out["fill"])
            posted += 1
        except SignalRoomError as e:
            if e.status == 409:
                log.info("%s: open signal still pending", pair)
            else:
                log.warning("%s: %s", pair, e)
        except Exception as e:   # a timeout on one coin must not cost the others their turn
            log.warning("%s: post failed (%s)", pair, type(e).__name__)
    return posted


def step(client: SignalRoomClient, done: int | None, clock=time.time, sleep=time.sleep) -> int:
    """Wait for the next window's moment and run it once. Returns that window, to be passed back as
    `done`. The time is read again after the wait: a machine that slept through the moment misses it."""
    w = next_window(clock(), done)
    sleep(max(0.0, w + ACT_AT - clock()))
    try:
        run_slot(client, w, clock=clock)
    except Exception as e:   # server restart, network blip: skip the slot, keep the schedule
        log.warning("slot %s failed (%s)", time.strftime("%H:%M", time.gmtime(w)), type(e).__name__)
    return w


def main(base_url: str, state_path: str) -> None:
    client = SignalRoomClient.load_or_register(base_url, state_path, name=NAME)
    log.info("favourite control running as %s", client.agent_id)
    done = None
    while True:
        done = step(client, done)

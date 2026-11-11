"""Kakalshi bridge: turns Kakalshi's paper bets into priced calls in the Signal Room.

Watches shadow_results.json. Each new bet on one of Kalshi's 15-minute crypto markets becomes one
priced call (rule set 4):
  ticker                -> the market, as is (KXSOL15M-26NOV111345-45)
  side yes | no         -> the side
  price_paid (cents)    -> the limit: the room fills the call only if, when it looks, the price is
                           still at or under what Kakalshi paid. No chasing.
  stated chance         -> the price paid plus 5 points, never above 0.99. Kakalshi's records carry
                           no chance of their own, so this is the bridge's fixed claim: "worth five
                           points more than it cost". It moves credits and ranking only; the verdict
                           depends on wins, losses and cost alone.

The room reads the price itself the moment the call lands, so these seconds matter: the file is
checked once a second, and a bet first seen more than 30 seconds after Kakalshi made it is not
posted at all (the price it got is gone, and a late fill would be a different bet).

Timestamps in the shadow log are UTC (Kalshi's clock); the bridge compares in UTC. Only bets made
after the bridge started are posted, so an old log does not flood the room. Posted bets are
remembered by id + timestamp, because Kakalshi restarts bet numbering whenever it resets its log.

Transient errors (rate limit, server restart, the last window's call still closing) are retried
every few seconds, until the bet is too old. Permanent rejections are recorded and skipped. The bet's
age is measured at the moment of each post, not once per look at the file.

Each Kakalshi strategy gets its own Signal Room agent (own wallet, own record), named
`kakalshi-<strategy>-priced`; the wallet is kept in the state directory under that name. The plain
calls this bridge posted before rule set 4 belong to `kakalshi-<strategy>`, whose record stays as it
is and receives nothing new.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import time

from signal_room import kalshi

from .client import SignalRoomClient, SignalRoomError

log = logging.getLogger("agents.kakalshi")

STATED_EDGE = 0.05       # the chance it states: what it paid plus 5 points
MAX_STATED = 0.99
MAX_AGE_SECONDS = 30     # a bet first seen later than this is not posted
POLL_SECONDS = 1.0
TRANSIENT_STATUSES = {401, 409, 429, 503}  # clock skew, last call still closing, rate limit, restart: try again
RETRY_SECONDS = 5        # how long a bet that hit a transient error waits before its next try


def load_posted(path: str) -> set:
    try:
        with open(path) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):   # missing, torn, or not a list of names
        return set()


def save_posted(path: str, posted: set) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)   # first run on a fresh clone
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(posted), f)
    os.replace(tmp, path)


def parse_bet(bet: dict) -> tuple[str, str, float, float] | None:
    """(market, side, limit, stated chance) for a bet the room can take as a priced call, else None."""
    market = bet.get("ticker", "")
    if not kalshi.parse_ticker(market):
        return None
    side = bet.get("side")
    if side not in ("yes", "no"):
        return None
    try:
        limit = round(float(bet.get("price_paid")) / 100.0, 4)
    except (TypeError, ValueError):
        return None
    if not (0.0 < limit < 1.0):
        return None
    return market, side, limit, round(min(MAX_STATED, limit + STATED_EDGE), 4)


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
    bets = data.get("bets") if isinstance(data, dict) else data
    return [b for b in bets if isinstance(b, dict)] if isinstance(bets, list) else []


def strategy_of(bet: dict) -> str:
    name = str(bet.get("strategy") or bet.get("source") or "unknown")
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "unknown"


def handle(bet: dict, now, started: dt.datetime, posted: set, client_for, holdoff: dict | None = None) -> bool:
    """Deal with one bet from the log. Returns True if it should be looked at again (a transient failure).
    now: the time, or a function giving it (so each bet's age is taken when its turn comes, not when the
    file was read). holdoff: bet -> when it may next be tried, so a retry is not attempted every second."""
    bid = f"{bet.get('id')}@{bet.get('timestamp', '')}"
    if bid in posted:
        return False
    clock = now if callable(now) else (lambda: now)
    now = clock()
    if holdoff is not None and bid in holdoff and now < holdoff[bid]:
        return True
    try:
        ts = dt.datetime.fromisoformat(bet["timestamp"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
    except Exception:
        log.warning("bet %s: its timestamp cannot be read; skipped", bid)
        posted.add(bid)
        return False
    if ts < started:
        posted.add(bid)  # history, not a live call
        return False
    parsed = parse_bet(bet)
    if parsed is None:
        posted.add(bid)
        return False
    age = (now - ts).total_seconds()
    if age < -MAX_AGE_SECONDS:
        log.warning("bet %s: stamped %.0fs in the future; skipped", bid, -age)
        posted.add(bid)
        return False
    if age > MAX_AGE_SECONDS:
        log.info("bet %s: first seen %.0fs after it was made, too late to post", bid, age)
        posted.add(bid)
        return False
    market, side, limit, stated = parsed
    strategy = strategy_of(bet)
    try:
        out = client_for(strategy).post_priced(market, side, limit, stated)
        log.info("bet %s [%s] -> %s %s limit=%.2f %s id=%s", bid, strategy, market, side, limit, out["fill"], out["signal_id"])
        posted.add(bid)
        return False
    except SignalRoomError as e:
        if e.status in TRANSIENT_STATUSES:
            log.info("bet %s: %s, will retry", bid, e)
            if holdoff is not None:
                holdoff[bid] = clock() + dt.timedelta(seconds=RETRY_SECONDS)   # from the end of the try
            return True
        log.warning("bet %s: rejected: %s", bid, e)
        posted.add(bid)
        return False
    except Exception as e:  # network blip, server restart: retry shortly
        log.warning("bet %s: post failed (%s), will retry", bid, type(e).__name__)
        if holdoff is not None:
            holdoff[bid] = clock() + dt.timedelta(seconds=RETRY_SECONDS)   # from the end of the try
        return True


def main(base_url: str, shadow_path: str, state_path: str, poll_seconds: float = POLL_SECONDS) -> None:
    state_dir = os.path.dirname(state_path) or "."
    clients: dict[str, SignalRoomClient] = {}

    def client_for(strategy: str) -> SignalRoomClient:
        if strategy not in clients:
            name = f"kakalshi-{strategy}-priced"
            clients[strategy] = SignalRoomClient.load_or_register(base_url, os.path.join(state_dir, f"{name}.json"), name=name)
            log.info("strategy %s -> agent %s", strategy, clients[strategy].agent_id)
        return clients[strategy]

    posted_path = state_path + ".posted.json"
    posted: set = load_posted(posted_path)
    started = dt.datetime.now(dt.timezone.utc)
    log.info("kakalshi adapter watching %s (one agent per strategy)", shadow_path)
    waiting_logged = False
    seen, retry, holdoff = None, False, {}
    while True:
        try:
            stamp = (os.path.getmtime(shadow_path), os.path.getsize(shadow_path))
        except OSError:
            if not waiting_logged:
                log.info("waiting for %s to appear (Kakalshi creates it on its first bet)", shadow_path)
                waiting_logged = True
            time.sleep(poll_seconds)
            continue
        waiting_logged = False
        if stamp == seen and not retry:      # nothing new since the last look
            time.sleep(poll_seconds)
            continue
        try:
            bets = load_bets(shadow_path)
        except Exception as e:  # anything unexpected: log once per occurrence, keep going
            log.warning("could not read shadow log: %s", e)
            time.sleep(poll_seconds)
            continue
        before = len(posted)
        retry = False
        for bet in bets:
            retry = handle(bet, lambda: dt.datetime.now(dt.timezone.utc), started, posted, client_for, holdoff) or retry
        if not retry:
            holdoff.clear()
        # A torn read (Kakalshi mid-write) comes back empty: look again rather than trusting it.
        seen = stamp if bets else None
        if len(posted) != before:
            save_posted(posted_path, posted)
        time.sleep(poll_seconds)

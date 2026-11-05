"""Forecaster v2: does the first minute of a 15-minute window predict the rest of it?

One idea, stated before any result (the question under Kakalshi's first-minute strategy, measured without
Kalshi prices, fees or slippage). Every setting below is fixed; any change is a new bot under a new name.

Windows: the UTC quarter-hours (:00, :15, :30, :45), the same windows Kalshi's 15-minute markets use.
Schedule: one minute into each window, alternating pairs: BTC and ETH on even quarter-hours, SOL and XRP on
  odd ones. Each coin is called every 30 minutes, so its last 15-minute call has closed before the next one,
  and no slot is ever skipped by choice.
The one fact, per coin, for the window opening at W (p(t) = the 1-minute close at time t, s = stdev of the
  60 one-minute log returns ending at W+1m):
  f = ln(p(W+1m) / p(W)) / s, clipped to [-4, 4]
Outcome it learns: the log return from W+1m to W+16m, the stretch the room scores a 15-minute call made at
  W+1m on: up beyond +FLAT, down beyond -FLAT, skipped in between (the room's ±0.05% band, symmetric).
Model: logistic regression with no intercept, ridge penalty LAMBDA, refitted every slot on the last
  TRAIN_WINDOWS windows of all four coins whose outcome was already known (every window up to 15 minutes
  before this one). No intercept means no built-in lean: an upside-down price history flips every call.
Call: direction = the side the model puts above 50%; confidence = that probability, capped at CAP.
What the room scores: its window starts at the first reference sample after the post, which lands in the
  minute after W+1m, and runs one horizon from there (about W+2m to W+17m). The fact ends at W+1m, so the
  bot's information is at least a minute older than the window it is judged on. No lookahead is possible.

Prices: Coinbase's public 1-minute candles (no keys). No model calls, no randomness.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import time

import httpx

from .client import SignalRoomClient, SignalRoomError
from .forecaster import fit, sigmoid

log = logging.getLogger("agents.forecaster2")

NAME = "forecaster-v2"
PAIRS = ("BTC", "ETH", "SOL", "XRP")
GROUPS = (("BTC", "ETH"), ("SOL", "XRP"))   # even quarter-hours, odd quarter-hours
HORIZON = "15m"
WINDOW = 900             # seconds in a window
FIRST = 60               # the first minute
VOL_MINUTES = 60
TRAIN_WINDOWS = 1000     # windows per coin the model learns from (about 10 days)
LAMBDA = 10.0
CLIP = 4.0
FLAT = math.log(1.0005)
CAP = 0.90
POST_AFTER_SECONDS = 8   # past W+1m, so the first minute's candle is out at Coinbase
ROOM_START = 120         # the room's start sample lands in the minute after the post: W+2m (used by the history check)
MAX_WAIT_SECONDS = 50    # if it is not out by then, the slot is missed (never chosen)
HISTORY = TRAIN_WINDOWS * WINDOW + WINDOW + FIRST + VOL_MINUTES * 60 + 120   # seconds of closes kept


# ---------- the math (pure functions, tested by hand) ----------
# px maps a minute boundary (UTC epoch seconds) to the close of the 1-minute candle ending there.

def feature(px: dict[int, float], w: int) -> list[float] | None:
    """The one fact for the window opening at w. None if any close it needs is missing."""
    end = w + FIRST
    need = [end - 60 * k for k in range(VOL_MINUTES + 1)]
    if any(t not in px for t in need):
        return None
    rets = [math.log(px[t] / px[t - 60]) for t in need[:-1]]
    mean = sum(rets) / len(rets)
    s = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
    if s == 0:
        return None
    return [max(-CLIP, min(CLIP, math.log(px[end] / px[w]) / s))]


def outcome(px: dict[int, float], w: int) -> int | None:
    """1 if the price rose past the flat band from w+1m to w+16m, 0 if it fell past it, None otherwise."""
    a, b = px.get(w + FIRST), px.get(w + FIRST + WINDOW)
    if a is None or b is None:
        return None
    r = math.log(b / a)
    if r > FLAT:
        return 1
    if r < -FLAT:
        return 0
    return None


def training_set(series: dict[str, dict[int, float]], w_now: int,
                 train_windows: int = TRAIN_WINDOWS) -> tuple[list[list[float]], list[int]]:
    """Every (fact, outcome) from the windows before w_now. The newest, w_now - 15m, closed at w_now + 1m:
    exactly when the bot decides, so every outcome used is already known."""
    xs, ys = [], []
    for px in series.values():
        for k in range(train_windows, 0, -1):
            w = w_now - k * WINDOW
            f, y = feature(px, w), outcome(px, w)
            if f is not None and y is not None:
                xs.append(f)
                ys.append(y)
    return xs, ys


def call(w: list[float], f: list[float]) -> tuple[str, float] | None:
    """(direction, confidence) from the model's probability that the rest of the window goes up."""
    p = sigmoid(sum(wk * fk for wk, fk in zip(w, f)))
    if p == 0.5:
        return None
    return ("long", min(CAP, p)) if p > 0.5 else ("short", min(CAP, 1 - p))


def known_before(px: dict[int, float], cutoff: int) -> dict[int, float]:
    return {t: c for t, c in px.items() if t <= cutoff}


def decide(series: dict[str, dict[int, float]], w_now: int, pairs: tuple[str, ...],
           start: list[float] | None = None) -> tuple[dict[str, tuple[str, float]], list[float] | None]:
    """Fit on what was known at w_now + 1m, then call each pair. Returns (calls, weights)."""
    seen = {p: known_before(px, w_now + FIRST) for p, px in series.items()}
    xs, ys = training_set(seen, w_now)
    if len(xs) < 100:
        return {}, None
    w = fit(xs, ys, lam=LAMBDA, start=start)
    out = {}
    for pair in pairs:
        f = feature(seen.get(pair, {}), w_now)
        c = call(w, f) if f is not None else None
        if c is not None:
            out[pair] = c
    return out, w


def group_for(w: int) -> tuple[str, ...]:
    return GROUPS[(w // WINDOW) % 2]


# ---------- data and schedule ----------

def fetch_minutes(http: httpx.Client, pair: str, since: int, until: int) -> dict[int, float]:
    """1-minute closes keyed by the minute they close at, for candles closing in (since, until]."""
    out: dict[int, float] = {}
    t = since
    while t < until:
        stop = min(until, t + 300 * 60)
        r = http.get(f"https://api.exchange.coinbase.com/products/{pair}-USD/candles", timeout=15,
                     params={"granularity": 60,
                             "start": dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                             "end": dt.datetime.fromtimestamp(stop - 60, dt.timezone.utc).isoformat().replace("+00:00", "Z")})
        r.raise_for_status()
        for row in r.json():   # [start time, low, high, open, close, volume]
            close_at = int(row[0]) + 60
            if since < close_at <= until:
                out[close_at] = float(row[4])
        t = stop
        time.sleep(0.4)
    return out


class Closes:
    """A rolling store of each coin's 1-minute closes: filled once, then topped up each slot."""
    def __init__(self) -> None:
        self.px: dict[str, dict[int, float]] = {p: {} for p in PAIRS}

    def top_up(self, http: httpx.Client, until: int) -> None:
        for pair in PAIRS:
            px = self.px[pair]
            since = max(max(px) if px else 0, until - HISTORY)
            px.update(fetch_minutes(http, pair, since, until))
            for t in [t for t in px if t < until - HISTORY]:
                del px[t]


def run_slot(client: SignalRoomClient, http: httpx.Client, store: Closes, w: int) -> None:
    pairs = group_for(w)
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    while True:
        try:
            store.top_up(http, w + FIRST)
        except Exception as e:
            log.warning("candle fetch failed: %s", e)
        if all(w + FIRST in store.px[p] for p in pairs) or time.monotonic() > deadline:
            break
        time.sleep(5)
    if time.time() > w + FIRST + POST_AFTER_SECONDS + MAX_WAIT_SECONDS:
        log.warning("missed the %s UTC slot: prices arrived too late", time.strftime("%H:%M", time.gmtime(w)))
        return   # a late call would start from a different price than the one the model learned from
    calls, _ = decide(store.px, w, pairs)
    if not calls:
        log.warning("missed the %s UTC slot: first-minute candles not out", time.strftime("%H:%M", time.gmtime(w)))
        return
    for pair, (direction, confidence) in calls.items():
        try:
            out = client.post_signal(pair, HORIZON, direction, round(confidence, 4))
            log.info("%s %s posted -> %s", pair, direction, out["signal_id"])
        except SignalRoomError as e:
            log.warning("%s: %s", pair, e)


def main(base_url: str, state_path: str) -> None:
    client = SignalRoomClient.load_or_register(base_url, state_path, name=NAME)
    http = httpx.Client()
    store = Closes()
    log.info("forecaster v2 running as %s; loading about 10 days of 1-minute prices", client.agent_id)
    while True:
        try:
            store.top_up(http, int(time.time() // 60) * 60)
            break
        except Exception as e:
            log.warning("price history load failed, retrying in a minute: %s", e)
            time.sleep(60)
    done = None
    while True:
        w = next_window(time.time(), done)
        time.sleep(max(0.0, w + FIRST + POST_AFTER_SECONDS - time.time()))
        try:
            run_slot(client, http, store, w)
        except Exception as e:   # server restart, network blip: skip the slot, keep the schedule
            log.warning("slot %s failed (%s)", time.strftime("%H:%M", time.gmtime(w)), type(e).__name__)
        done = w


def next_window(now: float, done: int | None) -> int:
    """The next slot to run: this window's if its slot is still open and not yet run, else the next one."""
    w = int(now // WINDOW) * WINDOW
    if now > w + FIRST + POST_AFTER_SECONDS + MAX_WAIT_SECONDS or (done is not None and w <= done):
        w = max(w, done or w) + WINDOW
    return w

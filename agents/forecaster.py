"""Forecaster v1: a statistical forecaster built to be judged, not to look good.

Everything below is fixed before the bot saw a single result. Any change to it is a new bot under a new
name (forecaster-v2, ...) with a fresh record, so the settings can never be tuned until the record looks good.

What it calls: each coin's direction over the next hour (1h horizon).
Schedule: on the hour, alternating pairs: BTC and ETH on even UTC hours, SOL and XRP on odd ones. Each coin
  is called every two hours, so its last call has closed before the next one, every slot lines up exactly
  with an hourly candle (what it predicts is what it learned from), and no slot is ever skipped by choice.
Facts it uses, per coin, at the close of hour t (c = hourly close, s = stdev of the last 24 hourly log returns):
  f1 = ln(c_t / c_t-1) / s
  f2 = ln(c_t / c_t-4) / (2 s)
  f3 = ln(c_t / c_t-24) / (sqrt(24) s)
  Each is about one unit wide when prices wander at random; each is clipped to [-4, 4].
Model: logistic regression with no intercept, ridge penalty LAMBDA, fitted by Newton's method on the last
  TRAIN_HOURS hours of all four coins pooled. Refitted every slot, on hours whose outcome was already known.
  With no intercept the model has no built-in lean: turn the price history upside down and every call flips,
  with the same confidence. A long-run drift cannot creep in as a default bet.
Outcome it learns: up if the next hour's log return is above +FLAT, down if below -FLAT, skipped in between
  (the room's own flat band, ±0.05%, applied symmetrically).
Call: direction = the side the model puts above 50%; confidence = that probability, capped at CAP.

Prices come from Coinbase's public hourly candles (no keys). No model calls, no randomness.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import time

import httpx

from .client import SignalRoomClient, SignalRoomError

log = logging.getLogger("agents.forecaster")

NAME = "forecaster-v1"
PAIRS = ("BTC", "ETH", "SOL", "XRP")
GROUPS = (("BTC", "ETH"), ("SOL", "XRP"))   # even UTC hours, odd UTC hours
HORIZON = "1h"
LOOKBACK = 24            # hours of history each fact needs
TRAIN_HOURS = 1000       # hours per coin the model learns from
LAMBDA = 10.0            # ridge penalty
CLIP = 4.0
FLAT = math.log(1.0005)  # the room's ±0.05% flat band, as a symmetric log return
CAP = 0.90
POST_AFTER_SECONDS = 40  # wait this long past the hour so the hour's candle has closed at Coinbase
MAX_WAIT_SECONDS = 300   # if the candle is not out by then, the slot is missed (never chosen)


# ---------- the math (pure functions, tested by hand) ----------

def features(closes: list[float | None], t: int) -> list[float] | None:
    """The three facts at the close of hour t. None if any of the 25 closes they need is missing."""
    if t < LOOKBACK or any(c is None for c in closes[t - LOOKBACK:t + 1]):
        return None
    rets = [math.log(closes[j] / closes[j - 1]) for j in range(t - LOOKBACK + 1, t + 1)]
    mean = sum(rets) / len(rets)
    s = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
    if s == 0:
        return None
    raw = (math.log(closes[t] / closes[t - 1]) / s,
           math.log(closes[t] / closes[t - 4]) / (2 * s),
           math.log(closes[t] / closes[t - 24]) / (math.sqrt(24) * s))
    return [max(-CLIP, min(CLIP, x)) for x in raw]


def outcome(closes: list[float | None], t: int) -> int | None:
    """1 if hour t+1 went up past the flat band, 0 if down past it, None if flat, missing or not known yet."""
    if t + 1 >= len(closes) or closes[t] is None or closes[t + 1] is None:
        return None
    r = math.log(closes[t + 1] / closes[t])
    if r > FLAT:
        return 1
    if r < -FLAT:
        return 0
    return None


def training_set(series: dict[str, list[float | None]], train_hours: int = TRAIN_HOURS) -> tuple[list[list[float]], list[int]]:
    """Every (facts, outcome) pair whose outcome is already known, from each coin's last train_hours hours."""
    xs, ys = [], []
    for closes in series.values():
        last = len(closes) - 2   # the newest hour with a known outcome
        for t in range(max(LOOKBACK, last - train_hours + 1), last + 1):
            f, y = features(closes, t), outcome(closes, t)
            if f is not None and y is not None:
                xs.append(f)
                ys.append(y)
    return xs, ys


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (the system is small and positive definite)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            k = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= k * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def fit(xs: list[list[float]], ys: list[int], lam: float = LAMBDA, iters: int = 50,
        start: list[float] | None = None) -> list[float]:
    """Ridge logistic regression, no intercept: minimise sum of log-loss + lam/2 * |w|^2 (Newton's method).
    The problem has one minimum, so `start` (a warm start for the history check) changes speed, not the answer."""
    d = len(xs[0]) if xs else 3
    w = list(start) if start else [0.0] * d
    for _ in range(iters):
        grad = [lam * w[k] for k in range(d)]
        hess = [[lam if i == j else 0.0 for j in range(d)] for i in range(d)]
        for x, y in zip(xs, ys):
            p = sigmoid(sum(wk * xk for wk, xk in zip(w, x)))
            for i in range(d):
                grad[i] += (p - y) * x[i]
                for j in range(d):
                    hess[i][j] += p * (1 - p) * x[i] * x[j]
        step = solve(hess, grad)
        w = [wk - sk for wk, sk in zip(w, step)]
        if max(abs(s) for s in step) < 1e-10:
            break
    return w


def call(w: list[float], f: list[float]) -> tuple[str, float] | None:
    """(direction, confidence) from the model's probability that the next hour goes up."""
    p = sigmoid(sum(wk * fk for wk, fk in zip(w, f)))
    if p == 0.5:
        return None
    return ("long", min(CAP, p)) if p > 0.5 else ("short", min(CAP, 1 - p))


def decide(series: dict[str, list[float | None]], pairs: tuple[str, ...]) -> dict[str, tuple[str, float]]:
    """Fit on everything already known, then call each pair from its newest completed hour."""
    xs, ys = training_set(series)
    if len(xs) < 100:
        return {}
    w = fit(xs, ys)
    out = {}
    for pair in pairs:
        closes = series.get(pair) or []
        f = features(closes, len(closes) - 1)
        c = call(w, f) if f is not None else None
        if c is not None:
            out[pair] = c
    return out


# ---------- data and schedule ----------

def fetch_closes(http: httpx.Client, pair: str, end: dt.datetime, hours: int) -> list[float | None] | None:
    """Hourly closes for the `hours` completed hours ending at `end` (an hour boundary), oldest first.
    An hour Coinbase has no candle for stays None, so no fact ever spans a gap. None if the hour that
    just ended is not out yet."""
    want = [end - dt.timedelta(hours=h) for h in range(hours, 0, -1)]   # each hour's start time
    got: dict[int, float] = {}
    chunk = 300
    for i in range(0, len(want), chunk):
        part = want[i:i + chunk]
        r = http.get(f"https://api.exchange.coinbase.com/products/{pair}-USD/candles", timeout=15,
                     params={"granularity": 3600, "start": part[0].isoformat() + "Z",
                             "end": (part[-1]).isoformat() + "Z"})
        r.raise_for_status()
        for row in r.json():   # [time, low, high, open, close, volume]
            got[int(row[0])] = float(row[4])
        time.sleep(0.4)        # stay well inside Coinbase's public rate limit
    stamps = [int(t.replace(tzinfo=dt.timezone.utc).timestamp()) for t in want]
    if stamps[-1] not in got:
        return None
    return [got.get(s) for s in stamps]


def next_slot(now: dt.datetime) -> dt.datetime:
    return now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)


def run_slot(client: SignalRoomClient, http: httpx.Client, hour: dt.datetime) -> None:
    pairs = GROUPS[hour.hour % 2]
    series: dict[str, list[float | None]] = {}
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    for pair in PAIRS:
        while True:
            try:
                closes = fetch_closes(http, pair, hour, TRAIN_HOURS + LOOKBACK + 2)
            except Exception as e:
                closes = None
                log.warning("%s: candle fetch failed: %s", pair, e)
            if closes is not None or time.monotonic() > deadline:
                break
            time.sleep(20)
        if closes is None:
            log.warning("missed the %s:00 UTC slot: %s history incomplete", hour.strftime("%H"), pair)
            return
        series[pair] = closes
    for pair, (direction, confidence) in decide(series, pairs).items():
        try:
            out = client.post_signal(pair, HORIZON, direction, round(confidence, 4))
            log.info("%s %s posted -> %s", pair, direction, out["signal_id"])
        except SignalRoomError as e:
            log.warning("%s: %s", pair, e)


def main(base_url: str, state_path: str) -> None:
    client = SignalRoomClient.load_or_register(base_url, state_path, name=NAME)
    http = httpx.Client()
    log.info("forecaster v1 running as %s", client.agent_id)
    while True:
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        slot = next_slot(now)
        time.sleep(max(0.0, (slot - now).total_seconds() + POST_AFTER_SECONDS))
        try:
            run_slot(client, http, slot)
        except Exception as e:   # server restart, network blip: skip the slot, keep the schedule
            log.warning("slot %s failed (%s)", slot.strftime("%H:%M"), type(e).__name__)

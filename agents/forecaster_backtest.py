"""History check for forecaster v1: `./start.sh backtest`.

Replays the last TEST_HOURS hours exactly as the live bot would have called them: same schedule (two coins
per hour, alternating), same facts, same model refitted at every slot on what was known at that moment, same
settings. Nothing is tuned here; the settings are imported from the live bot. Calls are scored with the room's
own scoring code (flat band, Brier, batches, luck odds), so the numbers read like a room record.

This is not proof. It is history the bot's design could, in principle, have been fitted to; only the room's
forward record counts. It answers one question: does the bot start out ahead of a coin flip, or not.

Scoring follows the room's rule set 3: a call's window starts at the first reference sample taken after the
post. For v2 that is the minute after the post (scored from W+2m to W+17m, while its one fact ends at W+1m).
For v1 it is about a minute into the hour; with hourly candles the replay scores from the hour itself, one
minute early, which is the one place the replay is not exactly live.
"""
from __future__ import annotations

import datetime as dt
import sys

import httpx

from signal_room import scoring

from . import forecaster as fc
from . import forecaster_v2 as f2

TEST_HOURS = 2000   # about 83 days
TEST_WINDOWS_V2 = 2000   # quarter-hour slots, about 21 days


def precompute(closes: list[float | None]) -> tuple[list, list]:
    return [fc.features(closes, t) for t in range(len(closes))], [fc.outcome(closes, t) for t in range(len(closes))]


def replay(series: dict[str, list[float | None]], end: dt.datetime, test_hours: int = TEST_HOURS) -> list[dict]:
    """One dict per call the live bot would have made. series[pair][i] is the close at end - (N-1-i) hours."""
    n = len(next(iter(series.values())))
    pre = {p: precompute(c) for p, c in series.items()}
    calls, w = [], None
    for i in range(n - 1 - test_hours, n - 1):          # each slot needs the next hour to score it
        hour = end - dt.timedelta(hours=n - 1 - i)
        # What training_set(series cut at i) would build: outcomes known by hour i only.
        xs, ys = [], []
        for p in fc.PAIRS:
            feats, outs = pre[p]
            for t in range(max(fc.LOOKBACK, i - 1 - fc.TRAIN_HOURS + 1), i):
                if feats[t] is not None and outs[t] is not None:
                    xs.append(feats[t])
                    ys.append(outs[t])
        if len(xs) < 100:
            continue
        w = fc.fit(xs, ys, start=w)
        for pair in fc.GROUPS[hour.hour % 2]:
            f = pre[pair][0][i]
            c = fc.call(w, f) if f is not None else None
            start_px, end_px = series[pair][i], series[pair][i + 1]
            if c is None or end_px is None:
                continue
            direction, confidence = c
            result, y = scoring.outcome(direction, start_px, end_px)
            calls.append({"slot": i, "hour": hour, "pair": pair, "direction": direction, "confidence": confidence,
                          "result": result, "brier": scoring.brier(confidence, y),
                          "net_return": scoring.net_return(direction, start_px, end_px),
                          "long_return": scoring.net_return("long", start_px, end_px)})
    return calls


def replay_v2(series: dict[str, dict[int, float]], first_w: int, last_w: int) -> list[dict]:
    """v2's calls for every slot from first_w to last_w (window opens, UTC seconds), as live would make them.
    Facts and outcomes are computed once per window (each uses only its own window's prices), then every slot
    learns from exactly the windows live would have known."""
    pre = {}
    for p, px in series.items():
        lo = first_w - f2.TRAIN_WINDOWS * f2.WINDOW
        pre[p] = {w: (f2.feature(px, w), f2.outcome(px, w)) for w in range(lo, last_w + 1, f2.WINDOW)}
    calls, wts = [], None
    for w in range(first_w, last_w + 1, f2.WINDOW):
        xs, ys = [], []
        for p in f2.PAIRS:
            for k in range(f2.TRAIN_WINDOWS, 0, -1):
                f, y = pre[p][w - k * f2.WINDOW]
                if f is not None and y is not None:
                    xs.append(f)
                    ys.append(y)
        if len(xs) < 100:
            continue
        wts = fc.fit(xs, ys, lam=f2.LAMBDA, start=wts)
        for pair in f2.group_for(w):
            f = pre[pair][w][0]
            c = f2.call(wts, f) if f is not None else None
            # The room starts the window at its first sample after the post (W+1m+8s), i.e. in the next
            # minute, and ends it one horizon later. Score the same stretch, not the one the model saw.
            a, b = series[pair].get(w + f2.ROOM_START), series[pair].get(w + f2.ROOM_START + f2.WINDOW)
            if c is None or a is None or b is None:
                continue
            direction, confidence = c
            result, y = scoring.outcome(direction, a, b)
            calls.append({"slot": w, "hour": dt.datetime.fromtimestamp(w, dt.timezone.utc).replace(tzinfo=None),
                          "pair": pair, "direction": direction, "confidence": confidence, "result": result,
                          "brier": scoring.brier(confidence, y), "net_return": scoring.net_return(direction, a, b),
                          "long_return": scoring.net_return("long", a, b)})
    return calls


def summary(calls: list[dict], name: str = "Forecaster v1", unit: str = "hour") -> str:
    rec = scoring.record([c["brier"] for c in calls], [c["result"] for c in calls], [c["net_return"] for c in calls],
                         [c["slot"] for c in calls], 1, [c["long_return"] for c in calls])
    longs = sum(c["direction"] == "long" for c in calls)
    first, last = calls[0]["hour"], calls[-1]["hour"]
    lines = [
        f"{name} history check, {first:%b %d} to {last:%b %d %Y} (UTC), settings exactly as live",
        "",
        f"{len(calls)} calls, {rec.n} scored, {rec.flats} flat; {rec.tries} separate tries (two coins per {unit} count once)",
        f"right {rec.hit_rate * 100:.1f}% (range {rec.hit_rate_low * 100:.1f}–{rec.hit_rate_high * 100:.1f}%); a coin flip is 50%",
        f"skill vs a coin flip: {rec.skill_points:+d} points",
        f"luck odds: a coin-flipper does this well about 1 in {max(1, round(1 / rec.p_luck)):,}"
        if rec.p_luck < 0.5 else "luck odds: about even (indistinguishable from a coin flip)",
        f"verdict this record would get in the room: {rec.verdict.replace('_', ' ')}",
        f"called long {longs} times, short {len(calls) - longs} (no lean is built in; this split is what the data said)",
        f"if traded 1:1 at {scoring.config.ROUND_TRIP_COST * 100:.2f}% cost per trade: {rec.cum_return * 100:+.1f}%;"
        f" always-long over the same windows: {rec.always_long_return * 100:+.1f}%",
    ]
    judged = [b for b in rec.calibration if b["judged"] != "too few"]
    if judged:
        lines.append("calibration: " + "; ".join(
            f"said {b['band']}, right {b['actual'] * 100:.0f}% ({b['judged']})" for b in judged))
    lines += ["", "History is not proof: only the room's forward record counts. Nothing was tuned on this."]
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    which = argv[0] if argv else "v1"
    http = httpx.Client()
    if which == "v2":
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        last_w = (now // f2.WINDOW) * f2.WINDOW - 2 * f2.WINDOW          # newest window already fully scored
        first_w = last_w - (TEST_WINDOWS_V2 - 1) * f2.WINDOW
        since = first_w - f2.TRAIN_WINDOWS * f2.WINDOW - f2.VOL_MINUTES * 60 - 120
        until = last_w + f2.FIRST + f2.WINDOW
        days = (until - since) / 86400
        print(f"Fetching {days:.0f} days of 1-minute candles for {len(f2.PAIRS)} coins from Coinbase (several minutes)...")
        series = {p: f2.fetch_minutes(http, p, since, until) for p in f2.PAIRS}
        print("Replaying every slot...")
        print()
        print(summary(replay_v2(series, first_w, last_w), "Forecaster v2", "quarter-hour"))
        return
    end = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    hours = TEST_HOURS + fc.TRAIN_HOURS + fc.LOOKBACK + 2
    print(f"Fetching {hours:,} hours of candles for {len(fc.PAIRS)} coins from Coinbase (about a minute)...")
    series = {}
    for pair in fc.PAIRS:
        closes = fc.fetch_closes(http, pair, end, hours)
        if closes is None:
            sys.exit(f"Coinbase has not published the last hour for {pair} yet; try again in a minute.")
        series[pair] = closes
    print("Replaying every slot (a few minutes)...")
    print()
    print(summary(replay(series, end)))


if __name__ == "__main__":
    main(sys.argv[1:])

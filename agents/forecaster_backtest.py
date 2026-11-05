"""History check for forecaster v1: `./start.sh backtest`.

Replays the last TEST_HOURS hours exactly as the live bot would have called them: same schedule (two coins
per hour, alternating), same facts, same model refitted at every slot on what was known at that moment, same
settings. Nothing is tuned here; the settings are imported from the live bot. Calls are scored with the room's
own scoring code (flat band, Brier, batches, luck odds), so the numbers read like a room record.

This is not proof. It is history the bot's design could, in principle, have been fitted to; only the room's
forward record counts. It answers one question: does v1 start out ahead of a coin flip, or not.
"""
from __future__ import annotations

import datetime as dt
import sys

import httpx

from signal_room import scoring

from . import forecaster as fc

TEST_HOURS = 2000   # about 83 days


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


def summary(calls: list[dict]) -> str:
    rec = scoring.record([c["brier"] for c in calls], [c["result"] for c in calls], [c["net_return"] for c in calls],
                         [c["slot"] for c in calls], 1, [c["long_return"] for c in calls])
    longs = sum(c["direction"] == "long" for c in calls)
    first, last = calls[0]["hour"], calls[-1]["hour"]
    lines = [
        f"Forecaster v1 history check, {first:%b %d} to {last:%b %d %Y} (UTC), settings exactly as live",
        "",
        f"{len(calls)} calls, {rec.n} scored, {rec.flats} flat; {rec.tries} separate tries (two coins per hour count once)",
        f"right {rec.hit_rate * 100:.1f}% (range {rec.hit_rate_low * 100:.1f}–{rec.hit_rate_high * 100:.1f}%); a coin flip is 50%",
        f"skill vs a coin flip: {rec.skill_points:+d} points",
        f"luck odds: a coin-flipper does this well about 1 in {max(1, round(1 / rec.p_luck)):,}"
        if rec.p_luck < 0.5 else "luck odds: about even (indistinguishable from a coin flip)",
        f"verdict this record would get in the room: {rec.verdict.replace('_', ' ')}",
        f"called long {longs} times, short {len(calls) - longs} (no lean is built in; this split is what the data said)",
        f"if traded 1:1 at {scoring.config.ROUND_TRIP_COST * 100:.2f}% cost per trade: {rec.cum_return * 100:+.1f}%;"
        f" always-long over the same hours: {rec.always_long_return * 100:+.1f}%",
    ]
    judged = [b for b in rec.calibration if b["judged"] != "too few"]
    if judged:
        lines.append("calibration: " + "; ".join(
            f"said {b['band']}, right {b['actual'] * 100:.0f}% ({b['judged']})" for b in judged))
    lines += ["", "History is not proof: only the room's forward record counts. Nothing was tuned on this."]
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    end = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    hours = TEST_HOURS + fc.TRAIN_HOURS + fc.LOOKBACK + 2
    print(f"Fetching {hours:,} hours of candles for {len(fc.PAIRS)} coins from Coinbase (about a minute)...")
    http = httpx.Client()
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

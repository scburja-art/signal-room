"""Forecaster v2: the first-minute fact by hand, no built-in lean, no peeking past the first minute."""
import math

import pytest

from agents import forecaster_v2 as f2

W0 = 1_793_836_800   # 2026-11-04 00:00 UTC, a quarter-hour boundary


def minutes(n: int, seed: int = 5, start: float = 100.0, t0: int = W0) -> dict[int, float]:
    """A deterministic wiggly 1-minute price path: close at t0 + 60k for k = 0..n-1."""
    out, p, x = {}, start, seed
    for k in range(n):
        x = (x * 1103515245 + 12345) % 2**31
        p *= math.exp(((x / 2**31) - 0.5) * 0.004)
        out[t0 + 60 * k] = p
    return out


def test_feature_by_hand():
    # 61 closes ending at W+1m: returns alternate +a, -a (60 of them, mean 0, so s = a),
    # and the last one (the window's first minute, W -> W+1m) is +a. So the fact is exactly +1.
    w = W0 + 3600
    a = math.log(1.001)
    px, p = {w + 60 - 60 * 60: 100.0}, 100.0
    for k in range(59, -1, -1):
        p = p * (1.001 if k % 2 == 0 else 1 / 1.001)
        px[w + 60 - 60 * k] = p
    f = f2.feature(px, w)
    assert f == [pytest.approx(math.log(px[w + 60] / px[w]) / a)]
    assert f[0] == pytest.approx(1.0)
    del px[w - 600]
    assert f2.feature(px, w) is None             # a missing minute: no fact, never a guess


def test_outcome_is_the_stretch_the_room_scores():
    w = W0
    px = {w + 60: 100.0, w + 960: 100.06}
    assert f2.outcome(px, w) == 1                 # from W+1m to W+16m, up past the band
    px[w + 960] = 99.94
    assert f2.outcome(px, w) == 0
    px[w + 960] = 100.04
    assert f2.outcome(px, w) is None              # flat: not learned
    assert f2.outcome({w + 60: 100.0}, w) is None   # not known yet


def big_series():
    n = (f2.TRAIN_WINDOWS + 4) * 15 + 80
    return {p: minutes(n, seed=i + 2) for i, p in enumerate(f2.PAIRS)}


def latest_window(series):
    last = max(max(px) for px in series.values())
    return ((last - 16 * 60) // f2.WINDOW) * f2.WINDOW - f2.WINDOW


def test_no_built_in_lean_upside_down_flips_every_call():
    series = big_series()
    w = latest_window(series)
    calls, _ = f2.decide(series, w, f2.PAIRS)
    flipped, _ = f2.decide({p: {t: 1 / c for t, c in px.items()} for p, px in series.items()}, w, f2.PAIRS)
    assert set(calls) == set(flipped) == set(f2.PAIRS)
    for p in f2.PAIRS:
        assert {calls[p][0], flipped[p][0]} == {"long", "short"}
        assert calls[p][1] == pytest.approx(flipped[p][1], abs=1e-9)


def test_never_sees_past_the_first_minute():
    series = big_series()
    w = latest_window(series)
    calls, wts = f2.decide(series, w, f2.PAIRS)
    # Wild prices from W+1m+1s on, including the window's own outcome, must change nothing.
    future = {p: {**px, **{t: c * 50 for t, c in px.items() if t > w + f2.FIRST}} for p, px in series.items()}
    calls2, wts2 = f2.decide(future, w, f2.PAIRS)
    assert calls == calls2 and wts == wts2


def test_newest_training_window_closed_exactly_at_decision_time():
    series = big_series()
    w = latest_window(series)
    seen = {p: f2.known_before(px, w + f2.FIRST) for p, px in series.items()}
    # The window 15 minutes earlier is scored from w-14m to w+1m: known at w+1m, so it is used.
    assert w - f2.WINDOW + f2.FIRST in seen["BTC"] and w + f2.FIRST in seen["BTC"]
    assert f2.outcome(seen["BTC"], w) is None     # this window's own outcome is never available


def test_schedule_alternates_and_never_collides():
    assert f2.group_for(W0) == ("BTC", "ETH")
    assert f2.group_for(W0 + 900) == ("SOL", "XRP")
    assert f2.group_for(W0 + 1800) == ("BTC", "ETH")
    # A coin's 15-minute call made at W+1m closes at W+16m, before its next slot at W+31m.
    assert f2.FIRST + f2.WINDOW < 2 * f2.WINDOW + f2.FIRST


def test_history_check_matches_live():
    from agents import forecaster_backtest as bt
    series = big_series()
    last_w = latest_window(series)
    first_w = last_w - 7 * f2.WINDOW
    calls = bt.replay_v2(series, first_w, last_w)
    assert calls
    for c in calls:
        live, _ = f2.decide(series, c["slot"], (c["pair"],))
        assert live[c["pair"]][0] == c["direction"]
        assert live[c["pair"]][1] == pytest.approx(c["confidence"], abs=1e-9)
        assert c["pair"] in f2.group_for(c["slot"])


class FakeHttp:
    def __init__(self, closes):
        self.closes = closes   # close time -> price

    def get(self, url, params, timeout):
        import datetime as dt
        s = dt.datetime.fromisoformat(params["start"][:-1]).replace(tzinfo=dt.timezone.utc).timestamp()
        e = dt.datetime.fromisoformat(params["end"][:-1]).replace(tzinfo=dt.timezone.utc).timestamp()
        rows = [[t - 60, 0, 0, 0, c, 0] for t, c in self.closes.items() if s <= t - 60 <= e]

        class R:
            def raise_for_status(self): pass
            def json(self): return sorted(rows, reverse=True)
        return R()


def test_fetch_keeps_exactly_the_closed_minutes(monkeypatch):
    monkeypatch.setattr(f2.time, "sleep", lambda s: None)
    px = minutes(700)
    got = f2.fetch_minutes(FakeHttp(px), "BTC", W0 + 60 * 10, W0 + 60 * 650)
    assert min(got) == W0 + 60 * 11 and max(got) == W0 + 60 * 650
    assert all(got[t] == px[t] for t in got) and len(got) == 640


def test_each_slot_runs_once():
    # The live bug of 2026-11-04: after a slot ran, the loop picked the same window again until its slot
    # closed, re-posting (409) and then hitting the rate limit (429). One run per window.
    w = W0
    assert f2.next_window(w + 30, None) == w                     # before the slot: this window
    assert f2.next_window(w + 70, w) == w + f2.WINDOW              # just ran it: the next window
    assert f2.next_window(w + 200, None) == w + f2.WINDOW          # slot already closed: the next window
    assert f2.next_window(w + f2.WINDOW + 20, w) == w + f2.WINDOW  # next window, not yet run

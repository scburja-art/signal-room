"""Forecaster v1: the math by hand, no built-in lean, no peeking at the future."""
import datetime as dt
import math

import pytest

from agents import forecaster as fc


def walk(n: int, seed: int = 7, start: float = 100.0) -> list[float]:
    """A deterministic wiggly price path (no randomness library, so the test never changes)."""
    out, p, x = [], start, seed
    for _ in range(n):
        x = (x * 1103515245 + 12345) % 2**31
        p *= math.exp(((x / 2**31) - 0.5) * 0.02)
        out.append(p)
    return out


def test_features_by_hand():
    # 25 closes: flat at 100, alternating ±1% for the 24 returns, last close known.
    closes = [100.0]
    for i in range(24):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    # returns alternate +a, -a with a = ln 1.01, mean 0, so s = a exactly.
    a = math.log(1.01)
    f = fc.features(closes, 24)
    assert f[0] == pytest.approx(-a / a)                          # last hour was a -a step
    assert f[1] == pytest.approx(0.0, abs=1e-12)                  # 4 steps cancel
    assert f[2] == pytest.approx(0.0, abs=1e-12)                  # 24 steps cancel
    assert fc.features(closes, 23) is None                        # not enough history
    gap = closes[:]
    gap[10] = None
    assert fc.features(gap, 24) is None                           # never spans a missing hour


def test_outcome_uses_symmetric_flat_band():
    assert fc.outcome([100.0, 100.06], 0) == 1
    assert fc.outcome([100.0, 99.94], 0) == 0
    assert fc.outcome([100.0, 100.04], 0) is None                 # inside ±0.05%: flat, not learned
    assert fc.outcome([100.0], 0) is None                         # the future is not known


def test_fit_matches_hand_solution():
    # One fact, two points: x=+1 went up, x=-1 went down, lam=1. Setting the gradient to zero gives
    # 2*sigmoid(w) - 2 + w = 0, whose root is w = 0.67484 (bisection by hand).
    w = fc.fit([[1.0], [-1.0]], [1, 0], lam=1.0)
    assert 2 * fc.sigmoid(w[0]) - 2 + w[0] == pytest.approx(0.0, abs=1e-9)
    assert w[0] == pytest.approx(0.67484, abs=1e-4)


def test_solve_by_hand():
    assert fc.solve([[2.0, 1.0], [1.0, 3.0]], [3.0, 5.0]) == pytest.approx([0.8, 1.4])


def test_no_built_in_lean_upside_down_flips_every_call():
    series = {p: walk(fc.TRAIN_HOURS + 30, seed=i + 3) for i, p in enumerate(fc.PAIRS)}
    upside_down = {p: [1 / c for c in closes] for p, closes in series.items()}
    calls = fc.decide(series, fc.PAIRS)
    flipped = fc.decide(upside_down, fc.PAIRS)
    assert set(calls) == set(flipped) == set(fc.PAIRS)
    for p in fc.PAIRS:
        (d1, c1), (d2, c2) = calls[p], flipped[p]
        assert {d1, d2} == {"long", "short"}
        assert c1 == pytest.approx(c2, abs=1e-9)


def test_untrained_model_is_a_coin_flip_and_says_nothing():
    assert fc.call([0.0, 0.0, 0.0], [1.0, -2.0, 0.5]) is None
    assert fc.call([10.0, 0.0, 0.0], [4.0, 0.0, 0.0]) == ("long", fc.CAP)    # capped, never overclaims
    assert fc.call([10.0, 0.0, 0.0], [-4.0, 0.0, 0.0]) == ("short", fc.CAP)


def test_training_uses_only_known_outcomes():
    closes = walk(200)
    xs, ys = fc.training_set({"BTC": closes}, train_hours=1000)
    # The newest hour's outcome is the future: never a training row. Changing the close that would
    # follow it (appending a wild price) must change nothing that was learned before it.
    xs2, ys2 = fc.training_set({"BTC": closes + [closes[-1] * 5]}, train_hours=1000)
    assert xs2[:len(xs)] == xs and ys2[:len(ys)] == ys
    assert len(xs2) >= len(xs)


class FakeHttp:
    """Coinbase candles, including the hour still in progress and one from the future."""
    def __init__(self, end: dt.datetime, hours: int, missing: set[int] = frozenset()):
        self.rows = []
        for h in range(-1, hours + 1):            # h = -1: in progress; h = 0: hour just ended
            start = end - dt.timedelta(hours=h + 1) if h >= 0 else end
            if h in missing:
                continue
            ts = int(start.replace(tzinfo=dt.timezone.utc).timestamp())
            self.rows.append([ts, 0, 0, 0, 1000.0 + h if h >= 0 else 999999.0, 0])

    def get(self, url, params, timeout):
        s = dt.datetime.fromisoformat(params["start"][:-1]).replace(tzinfo=dt.timezone.utc).timestamp()
        e = dt.datetime.fromisoformat(params["end"][:-1]).replace(tzinfo=dt.timezone.utc).timestamp()
        rows = [r for r in self.rows if s <= r[0] <= e]

        class R:
            def raise_for_status(self): pass
            def json(self): return sorted(rows, reverse=True)
        return R()


def test_fetch_never_sees_the_hour_in_progress(monkeypatch):
    monkeypatch.setattr(fc.time, "sleep", lambda s: None)
    end = dt.datetime(2026, 11, 4, 14, 0)
    closes = fc.fetch_closes(FakeHttp(end, 400), "BTC", end, 350)
    assert len(closes) == 350
    assert closes[-1] == 1000.0                   # the hour that just ended, not the one in progress
    assert 999999.0 not in closes
    assert closes[0] == 1000.0 + 349              # oldest first


def test_fetch_keeps_gaps_as_gaps(monkeypatch):
    monkeypatch.setattr(fc.time, "sleep", lambda s: None)
    end = dt.datetime(2026, 11, 4, 14, 0)
    closes = fc.fetch_closes(FakeHttp(end, 400, missing={5}), "BTC", end, 50)
    assert closes[-6] is None and closes[-5] == 1004.0
    assert fc.fetch_closes(FakeHttp(end, 400, missing={0}), "BTC", end, 50) is None   # last hour not out yet


def test_schedule_alternates_pairs_and_never_collides():
    assert fc.GROUPS[dt.datetime(2026, 11, 4, 14).hour % 2] == ("BTC", "ETH")
    assert fc.GROUPS[dt.datetime(2026, 11, 4, 15).hour % 2] == ("SOL", "XRP")
    assert fc.next_slot(dt.datetime(2026, 11, 4, 14, 59, 59)) == dt.datetime(2026, 11, 4, 15)
    assert fc.next_slot(dt.datetime(2026, 11, 4, 15, 0, 1)) == dt.datetime(2026, 11, 4, 16)
    # Each coin is called every 2 hours with a 1-hour horizon: its last call closed an hour earlier.
    assert set(fc.GROUPS[0]) | set(fc.GROUPS[1]) == set(fc.PAIRS) and not set(fc.GROUPS[0]) & set(fc.GROUPS[1])


def test_history_check_replays_exactly_what_live_would_call():
    from agents import forecaster_backtest as bt
    n = fc.TRAIN_HOURS + fc.LOOKBACK + 2 + 40
    series = {p: walk(n, seed=i + 11) for i, p in enumerate(fc.PAIRS)}
    end = dt.datetime(2026, 11, 4, 12)
    calls = bt.replay(series, end, test_hours=40)
    assert calls
    for c in calls[:: max(1, len(calls) // 6)]:
        i = c["slot"]
        assert c["hour"] == end - dt.timedelta(hours=n - 1 - i)
        assert c["pair"] in fc.GROUPS[c["hour"].hour % 2]
        live = fc.decide({p: closes[: i + 1] for p, closes in series.items()}, (c["pair"],))[c["pair"]]
        assert live[0] == c["direction"] and live[1] == pytest.approx(c["confidence"], abs=1e-9)
    text = bt.summary(calls)
    assert "settings exactly as live" in text and "Nothing was tuned" in text

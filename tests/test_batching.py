"""Calls that share market time: counted as one try, and staked from one betting account. Every expected
value here is worked by hand."""
import datetime as dt

import pytest

from signal_room import scoring

T0 = dt.datetime(2026, 10, 31, 12, 0, 0)


def at(*seconds):
    return [T0 + dt.timedelta(seconds=s) for s in seconds]


def win(start_s, horizon_s):
    return (T0 + dt.timedelta(seconds=start_s), T0 + dt.timedelta(seconds=start_s + horizon_s))


def test_calls_that_share_market_time_count_once():
    H = 3600
    # Four coins posted 61 s apart with 1-hour windows all open while the first is open: one try.
    assert scoring.cluster_ids([win(0, H), win(61, H), win(122, H), win(183, H)]) == [0, 0, 0, 0]
    # The next try opens only once the first call of the batch has closed.
    assert scoring.cluster_ids([win(0, H), win(3599, H), win(3600, H)]) == [0, 0, 1]
    # Posted every 30 minutes: pairs of overlapping calls, two tries per two hours (reviewer's scenario).
    assert scoring.cluster_ids([win(0, H), win(1800, H), win(3601, H), win(5400, H)]) == [0, 0, 1, 1]
    # A 15-minute call and a 1-hour call on the same stretch: one try.
    assert scoring.cluster_ids([win(0, 900), win(10, H)]) == [0, 0]
    # Back-to-back hourly calls (each posted after the last closed): every call its own try.
    assert scoring.cluster_ids([win(0, H), win(3675, H), win(7350, H)]) == [0, 1, 2]
    # Unsorted input keeps its order.
    assert scoring.cluster_ids([win(3675, H), win(0, H), win(7350, H), win(5, H)]) == [1, 0, 2, 0]
    assert scoring.cluster_ids([]) == []


def mean_bankroll(rights, wrongs):
    """Hand formula for calls one after another: average over the stakes of (1+stake)^rights * (1-stake)^wrongs."""
    return sum((1 + s) ** rights * (1 - s) ** wrongs for s in scoring.STAKES) / len(scoring.STAKES)


def odds(hits):
    up, down = scoring.call_payoffs(hits)
    return scoring.luck_from(up, down)


def test_one_call_hand_values():
    # One right: the mean of 1 + stake over the seven stakes is 1.225 → odds 1/1.225. One wrong: no evidence.
    assert odds([1]) == pytest.approx((1 / 1.225, 1.0))
    assert odds([0]) == pytest.approx((1.0, 1 / 1.225))
    assert odds([]) == (1.0, 1.0)


def test_twelve_right_alone_and_as_three_windows_of_four():
    # 12 calls one after another, all right: mean((1+s)^12) = 32.13 → about 1 in 32.
    briers, results = [0.04] * 12, ["right"] * 12
    alone = scoring.record(briers, results, [0.0] * 12)
    assert alone.p_luck == pytest.approx(1 / mean_bankroll(12, 0), rel=1e-5) and alone.tries == 12
    assert 1 / alone.p_luck == pytest.approx(32.13, rel=1e-3)
    # The same 12 as three windows of four calls open together. Each window turns cash c into
    # c x (2 - (1-s)^4): the four stakes add up to 1 - (1-s)^4 of it and each comes back doubled.
    windows = [win(w * 900, 900) for w in range(3) for _ in range(4)]
    together = scoring.record(briers, results, [0.0] * 12, scoring.cluster_ids(windows), windows=windows)
    by_hand = sum((2 - (1 - s) ** 4) ** 3 for s in scoring.STAKES) / len(scoring.STAKES)
    assert together.p_luck == pytest.approx(1 / by_hand, rel=1e-5) and together.tries == 3
    assert alone.hit_rate == together.hit_rate == 1.0 and alone.bss == together.bss
    assert together.verdict == "unproven"               # 3 tries, under 10
    # Four correlated wins in one window are worth less than four wins in a row.
    assert together.p_luck > alone.p_luck


def test_a_split_window_is_close_to_a_wash():
    # Two calls open together, one right and one wrong, 10% stakes: 0.81 + 0.20 = 1.01 or 0.81 + 0.18 = 0.99.
    tl = scoring.Timeline([win(0, 900), win(1, 900)])
    import math
    assert math.exp(tl.log_cash([1, -1], 0.1)) == pytest.approx(1.01)
    assert math.exp(tl.log_cash([-1, 1], 0.1)) == pytest.approx(0.99)


def test_order_does_not_matter_for_calls_in_a_row_and_fading_loses_the_verdict():
    assert odds([1] * 5 + [0] * 2) == pytest.approx(odds([0, 1] * 2 + [1] * 3))
    # 10 right then 10 wrong: (1+s)^10 (1-s)^10 = (1-s^2)^10 < 1 → back to no evidence.
    assert odds([1] * 10 + [0] * 10)[0] == 1.0


def test_worse_than_chance_is_the_mirror():
    assert odds([0] * 12)[1] == pytest.approx(odds([1] * 12)[0])


def test_checking_constantly_cannot_manufacture_skill():
    """A coin-flipper checked after every call. The old one-look test calls it 'probably not luck'
    at some point far more often than 1 in 20; the betting test stays under 1 in 20."""
    import math
    import random

    def one_look_p(k, n):
        return sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n

    rng = random.Random(20261031)
    runs, steps = 300, 300
    fooled_old = fooled_new = 0
    for _ in range(runs):
        k, logs = 0, [0.0] * len(scoring.STAKES)       # calls in a row: each stake's cash is a running product
        hit_old = hit_new = False
        for t in range(1, steps + 1):
            right = rng.random() < 0.5
            k += right
            logs = [v + math.log1p(s if right else -s) for v, s in zip(logs, scoring.STAKES)]
            if not hit_old and t % 5 == 0 and one_look_p(k, t) < 0.05:
                hit_old = True
            if not hit_new and math.exp(-scoring._log_mean(logs)) < 0.05:
                hit_new = True
        fooled_old += hit_old
        fooled_new += hit_new
    assert fooled_new / runs <= 0.05
    assert fooled_old / runs > 0.15          # the problem the betting test fixes


def test_batches_only_count_tries():
    # The same calls with and without batch ids: every number but the count of tries is the same.
    briers = [0.2025] * 8 + [0.3025] * 4
    results = ["right"] * 8 + ["wrong"] * 4
    alone = scoring.record(briers, results, [0.0] * 12)
    batched = scoring.record(briers, results, [0.0] * 12, clusters=[0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2])
    assert (alone.tries, batched.tries) == (12, 3)
    assert (batched.p_luck, batched.hit_rate_low, batched.bss_low) == (alone.p_luck, alone.hit_rate_low, alone.bss_low)


def test_old_rule_flats_no_longer_count():
    # A flat scored under rule set 1 carries Brier 0.0025 (a 0.55 call treated as half right).
    # It used to pull skill up; now only right/wrong calls are scored.
    briers = [0.2025, 0.3025, 0.0025]
    results = ["right", "wrong", "flat"]
    rec = scoring.record(briers, results, [0.0] * 3)
    assert rec.n == 2 and rec.flats == 1
    assert rec.bss == pytest.approx(1 - (0.2025 + 0.3025) / 2 / 0.25)   # -0.01, was +0.32 with the flat

"""Calls posted together count as one try. Every expected value here is worked by hand."""
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
    """Hand formula: average over the five stakes of (1+stake)^rights * (1-stake)^wrongs."""
    stakes = (0.1, 0.2, 0.3, 0.4, 0.5)
    return sum((1 + s) ** rights * (1 - s) ** wrongs for s in stakes) / len(stakes)


def test_one_call_hand_values():
    # One right: mean(1.1, 1.2, 1.3, 1.4, 1.5) = 1.3 → odds 1/1.3. One wrong: bankroll 0.7 → no evidence.
    assert scoring.luck_odds([(1, 1)]) == pytest.approx((1 / 1.3, 1.0))
    assert scoring.luck_odds([(0, 1)]) == pytest.approx((1.0, 1 / 1.3))
    assert scoring.luck_odds([]) == (1.0, 1.0)


def test_three_batches_of_four_all_right():
    # 12 calls alone: mean((1+s)^12) = 221.79 / 5 = 44.36 → about 1 in 44.
    # As three batches each fully right: mean((1+s)^3) = 11.375 / 5 = 2.275 → about 1 in 2.3.
    alone = scoring.luck_odds([(1, 1)] * 12)[0]
    batched = scoring.luck_odds([(4, 4)] * 3)[0]
    assert alone == pytest.approx(1 / 44.358, rel=1e-3)
    assert batched == pytest.approx(1 / 2.275, rel=1e-6)


def test_mixed_batch_sizes():
    # A batch of 2 fully right counts like one right call; a split batch (1 of 2) is a wash.
    assert scoring.luck_odds([(2, 2), (1, 1)])[0] == pytest.approx(1 / mean_bankroll(2, 0))
    assert scoring.luck_odds([(1, 2), (1, 1)])[0] == pytest.approx(1 / 1.3)


def test_order_does_not_matter_and_fading_loses_the_verdict():
    assert scoring.luck_odds([(1, 1)] * 5 + [(0, 1)] * 2) == pytest.approx(scoring.luck_odds([(0, 1), (1, 1)] * 2 + [(1, 1)] * 3))
    # 10 right then 10 wrong: (1+s)^10 (1-s)^10 = (1-s^2)^10 < 1 → back to no evidence.
    assert scoring.luck_odds([(1, 1)] * 10 + [(0, 1)] * 10)[0] == 1.0


def test_worse_than_chance_is_the_mirror():
    assert scoring.luck_odds([(0, 1)] * 12)[1] == pytest.approx(scoring.luck_odds([(1, 1)] * 12)[0])


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
        calls, k = [], 0
        hit_old = hit_new = False
        for t in range(1, steps + 1):
            right = rng.random() < 0.5
            k += right
            calls.append((1 if right else 0, 1))
            if not hit_old and t % 5 == 0 and one_look_p(k, t) < 0.05:
                hit_old = True
            if not hit_new and scoring.luck_odds(calls)[0] < 0.05:
                hit_new = True
        fooled_old += hit_old
        fooled_new += hit_new
    assert fooled_new / runs <= 0.05
    assert fooled_old / runs > 0.15          # the problem the betting test fixes


def test_record_uses_batches_for_luck_and_verdict():
    # 12 right in 3 batches of 4: hit rate and skill unchanged, luck odds and evidence honest.
    briers, results = [0.04] * 12, ["right"] * 12
    alone = scoring.record(briers, results, [0.0] * 12)
    batched = scoring.record(briers, results, [0.0] * 12, clusters=[0] * 4 + [1] * 4 + [2] * 4)
    assert alone.hit_rate == batched.hit_rate == 1.0 and alone.bss == batched.bss
    assert alone.p_luck == pytest.approx(1 / 44.358, rel=1e-3) and alone.tries == 12
    assert batched.p_luck == pytest.approx(1 / 2.275, rel=1e-5) and batched.tries == 3
    assert batched.verdict == "unproven"               # 3 tries, under 10


def test_batching_only_ever_widens_the_ranges():
    # Batches that all went the same way: ranges must be at least as wide as treating calls alone.
    briers = [0.2025] * 8 + [0.3025] * 4
    results = ["right"] * 8 + ["wrong"] * 4
    clusters = [0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2]
    alone = scoring.record(briers, results, [0.0] * 12)
    batched = scoring.record(briers, results, [0.0] * 12, clusters=clusters)
    assert batched.hit_rate_low <= alone.hit_rate_low and batched.hit_rate_high >= alone.hit_rate_high
    assert batched.bss_low <= alone.bss_low and batched.bss_high >= alone.bss_high
    # every batch its own call → identical to no batching
    same = scoring.record(briers, results, [0.0] * 12, clusters=list(range(12)))  # each call alone
    assert (same.p_luck, same.hit_rate_low, same.bss_low) == (alone.p_luck, alone.hit_rate_low, alone.bss_low)


def test_old_rule_flats_no_longer_count():
    # A flat scored under rule set 1 carries Brier 0.0025 (a 0.55 call treated as half right).
    # It used to pull skill up; now only right/wrong calls are scored.
    briers = [0.2025, 0.3025, 0.0025]
    results = ["right", "wrong", "flat"]
    rec = scoring.record(briers, results, [0.0] * 3)
    assert rec.n == 2 and rec.flats == 1
    assert rec.bss == pytest.approx(1 - (0.2025 + 0.3025) / 2 / 0.25)   # -0.01, was +0.32 with the flat

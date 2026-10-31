"""Calls posted together count as one try. Every expected value here is worked by hand."""
import datetime as dt

import pytest

from signal_room import scoring

T0 = dt.datetime(2026, 10, 31, 12, 0, 0)


def at(*seconds):
    return [T0 + dt.timedelta(seconds=s) for s in seconds]


def test_batches_by_first_call_within_a_minute():
    # 0, 0.2, 0.4 s → batch 0; 70 s → batch 1; 200 s → batch 2. Unsorted input keeps its order.
    assert scoring.cluster_ids(at(0, 0.2, 0.4, 70, 200), 60) == [0, 0, 0, 1, 2]
    assert scoring.cluster_ids(at(70, 0, 200, 0.2), 60) == [1, 0, 2, 0]
    # anchored on the batch's first call, so a slow drip cannot chain into one endless batch
    assert scoring.cluster_ids(at(0, 50, 100, 150), 60) == [0, 0, 1, 1]


def test_single_calls_match_the_old_binomial_exactly():
    for k, n in [(0, 0), (3, 4), (18, 25), (43, 72), (68, 112)]:
        assert scoring.p_luck_batched([(1, 1)] * k + [(0, 1)] * (n - k)) == pytest.approx(scoring.p_luck(k, n))


def test_three_batches_of_four_all_right():
    # Independent: 12 of 12 = 1/4096. As three batches each +2: only all-plus reaches +6 → 1/8.
    assert scoring.p_luck(12, 12) == pytest.approx(1 / 4096)
    assert scoring.p_luck_batched([(4, 4)] * 3) == pytest.approx(1 / 8)


def test_mixed_batch_sizes():
    # A: 2 of 2 (d=+1), B: 1 of 1 (d=+0.5). Sums over sign flips: 1.5, 0.5, -0.5, -1.5 → p = 1/4.
    assert scoring.p_luck_batched([(2, 2), (1, 1)]) == pytest.approx(0.25)
    # A split batch (1 of 2) is worth 0 either way, so only B's flip matters → p = 1/2.
    assert scoring.p_luck_batched([(1, 2), (1, 1)]) == pytest.approx(0.5)


def test_record_uses_batches_for_luck_and_evidence():
    # 12 right in 3 batches of 4: hit rate and skill unchanged, luck odds and evidence honest.
    briers, results = [0.04] * 12, ["right"] * 12
    alone = scoring.record(briers, results, [0.0] * 12)
    batched = scoring.record(briers, results, [0.0] * 12, clusters=[0] * 4 + [1] * 4 + [2] * 4)
    assert alone.hit_rate == batched.hit_rate == 1.0 and alone.bss == batched.bss
    assert alone.p_luck == pytest.approx(1 / 4096, rel=1e-3) and alone.tries == 12
    assert batched.p_luck == pytest.approx(0.125) and batched.tries == 3
    assert batched.evidence == "insufficient"          # 3 tries, under 10


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
    same = scoring.record(briers, results, [0.0] * 12, clusters=list(range(12)))
    assert (same.p_luck, same.hit_rate_low, same.bss_low) == (alone.p_luck, alone.hit_rate_low, alone.bss_low)


def test_old_rule_flats_no_longer_count():
    # A flat scored under rule set 1 carries Brier 0.0025 (a 0.55 call treated as half right).
    # It used to pull skill up; now only right/wrong calls are scored.
    briers = [0.2025, 0.3025, 0.0025]
    results = ["right", "wrong", "flat"]
    rec = scoring.record(briers, results, [0.0] * 3)
    assert rec.n == 2 and rec.flats == 1
    assert rec.bss == pytest.approx(1 - (0.2025 + 0.3025) / 2 / 0.25)   # -0.01, was +0.32 with the flat

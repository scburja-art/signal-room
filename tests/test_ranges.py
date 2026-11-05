"""Ranges that hold however often they are checked (approved by Steven 2026-11-04). Hand-computed."""
import math

import pytest

from signal_room import scoring

MEAN_STAKE = sum(scoring.STAKES) / len(scoring.STAKES)   # 0.3


def test_one_right_try_by_hand():
    # One try, right (value 1). The "above m" bankroll is the mean over stakes c of 1 + (c/m)(1 - m)
    # = 1 + 0.3 (1 - m)/m. It reaches 2/0.05 = 40 when (1 - m)/m = 130, i.e. m = 1/131.
    lo, hi = scoring.confidence_range([1.0], None)
    assert lo == pytest.approx(1 / 131, rel=1e-9)
    assert hi == pytest.approx(1.0, abs=1e-8)     # the "below m" bankroll is 1 - 0.3 = 0.7: never rules anything out


def test_mirror_image():
    vals = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 0.0, 1.0, 1.0, 0.0]
    lo, hi = scoring.confidence_range(vals, None)
    flo, fhi = scoring.confidence_range([1 - v for v in vals], None)
    assert flo == pytest.approx(1 - hi, abs=1e-9) and fhi == pytest.approx(1 - lo, abs=1e-9)


def test_edges_sit_exactly_on_the_bar():
    vals = [1.0] * 14 + [0.0] * 6
    lo, hi = scoring.confidence_range(vals, None)
    assert scoring._wealth(vals, lo, +1) == pytest.approx(40.0, rel=1e-6)
    assert scoring._wealth(vals, hi, -1) == pytest.approx(40.0, rel=1e-6)
    assert lo < 0.7 < hi
    # Hand check of the bankroll formula at one point: m = 0.5, value 1 multiplies by 1 + 2c(0.5), value 0 by 1 - c.
    by_hand = sum((1 + c) ** 14 * (1 - c) ** 6 for c in scoring.STAKES) / 5
    assert scoring._wealth(vals, 0.5, +1) == pytest.approx(by_hand)


def test_same_bankroll_as_the_luck_odds_at_a_coin_flip():
    hits = [1] * 30 + [0] * 12
    rec = scoring.record([0.16 if h else 0.36 for h in hits], ["right" if h else "wrong" for h in hits], [])
    assert scoring._wealth([float(h) for h in hits], 0.5, +1) == pytest.approx(1 / rec.p_luck, rel=1e-4)
    # So the range clears 50% exactly when the luck odds pass 1 in 40 (each side of the range gets half of 5%).
    assert (rec.hit_rate_low > 0.5) == (rec.p_luck < 1 / 40)


def test_a_batch_counts_once():
    alone = scoring.confidence_range([1.0], None)
    batch = scoring.confidence_range([1.0, 1.0, 1.0, 1.0], ["b", "b", "b", "b"])
    assert batch == pytest.approx(alone)


def test_slices_face_a_higher_bar():
    hits = [1] * 40 + [0] * 20
    briers = [0.16 if h else 0.36 for h in hits]
    results = ["right" if h else "wrong" for h in hits]
    whole, slice_of_4 = scoring.record(briers, results, []), scoring.record(briers, results, [], slices=4)
    assert slice_of_4.hit_rate_low < whole.hit_rate_low and slice_of_4.hit_rate_high > whole.hit_rate_high


def test_skill_range_from_brier_range():
    # Every call said 0.8 and was right: Brier 0.04 each, skill +0.84. The range brackets it.
    rec = scoring.record([0.04] * 40, ["right"] * 40, [])
    lo, hi = scoring.confidence_range([0.04] * 40, None)
    assert rec.bss_low == pytest.approx(round(1 - hi / 0.25, 4)) and rec.bss_high == pytest.approx(round(1 - lo / 0.25, 4))
    assert rec.bss_low < rec.bss < rec.bss_high


def test_empty():
    assert scoring.confidence_range([], None) == (0.0, 1.0)
    rec = scoring.record([], [], [])
    assert (rec.hit_rate_low, rec.hit_rate_high, rec.bss_low, rec.bss_high) == (0.0, 1.0, -3.0, 1.0)

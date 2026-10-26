"""Hand-computed checks of the scoring math."""
import pytest

from signal_room import scoring


def test_brier_hand_values():
    assert scoring.brier(0.80, 1.0) == pytest.approx(0.04)
    assert scoring.brier(0.80, 0.0) == pytest.approx(0.64)
    assert scoring.brier(0.50, 1.0) == pytest.approx(0.25)
    assert scoring.brier(0.50, 0.0) == pytest.approx(0.25)
    assert scoring.brier(1.00, 1.0) == pytest.approx(0.0)


def test_outcome_direction_and_flat_band():
    assert scoring.outcome("long", 100.0, 101.0) == ("right", 1.0)
    assert scoring.outcome("long", 100.0, 99.0) == ("wrong", 0.0)
    assert scoring.outcome("short", 100.0, 99.0) == ("right", 1.0)
    assert scoring.outcome("short", 100.0, 101.0) == ("wrong", 0.0)
    assert scoring.outcome("long", 100.0, 100.02) == ("flat", 0.5)   # +0.02%, inside ±0.05%
    assert scoring.outcome("long", 100.0, 100.06) == ("right", 1.0)  # +0.06%, outside


def test_credit_hand_values():
    assert scoring.credit_for(scoring.brier(1.0, 1.0), "right") == pytest.approx(1.0)   # perfect call = 1 credit
    assert scoring.credit_for(scoring.brier(0.5, 1.0), "right") == pytest.approx(0.0)   # coin flip = 0
    assert scoring.credit_for(scoring.brier(0.8, 0.0), "wrong") == pytest.approx(0.0)   # wrong = 0
    assert scoring.credit_for(scoring.brier(0.8, 1.0), "right") == pytest.approx(0.84)  # 4 * (0.25 - 0.04)
    assert scoring.credit_for(scoring.brier(0.6, 0.5), "flat") == pytest.approx(0.0)    # flat = 0


def test_skill_score_and_shrink():
    assert scoring.skill_score([0.25, 0.25, 0.25]) == pytest.approx(0.0)    # coin flips → 0
    assert scoring.skill_score([0.04, 0.04]) == pytest.approx(0.84)
    assert scoring.skill_score([0.64, 0.64]) == pytest.approx(-1.56)
    assert scoring.shrink(0.5, 30) == pytest.approx(0.25)   # half at n=30
    assert scoring.shrink(0.5, 300) == pytest.approx(0.5 * 300 / 330)


def test_net_return():
    assert scoring.net_return("long", 100.0, 101.0) == pytest.approx(0.009)   # +1% − 0.10%
    assert scoring.net_return("short", 100.0, 101.0) == pytest.approx(-0.011)


def test_random_agent_scores_near_zero():
    """Random 50/50 calls at 0.55 confidence: expected BSS ≈ 1 − 0.2525/0.25 = −0.01."""
    import random
    rng = random.Random(7)
    briers = []
    for _ in range(5000):
        y = 1.0 if rng.random() < 0.5 else 0.0
        briers.append(scoring.brier(0.55, y))
    assert abs(scoring.skill_score(briers) - (-0.01)) < 0.03


def test_hedging_nets_to_nothing():
    """Long and short on the same move: one right, one wrong, credits sum to zero at coin-flip confidence."""
    r1, y1 = scoring.outcome("long", 100.0, 102.0)
    r2, y2 = scoring.outcome("short", 100.0, 102.0)
    c = scoring.credit_for(scoring.brier(0.5, y1), r1) + scoring.credit_for(scoring.brier(0.5, y2), r2)
    assert c == pytest.approx(0.0)
    # At 0.8 confidence the hedge earns 0.84 on the right leg and 0 on the wrong, but BSS over both is negative
    assert scoring.skill_score([scoring.brier(0.8, y1), scoring.brier(0.8, y2)]) < 0


def test_record_and_delist():
    rec = scoring.record([0.04] * 40, ["right"] * 40, [0.01] * 40)
    assert rec.ranked and rec.n == 40
    assert rec.bss == pytest.approx(0.84)
    assert rec.score == pytest.approx(0.84 * 40 / 70, abs=1e-4)
    assert rec.hit_rate == 1.0
    assert not scoring.should_delist([0.64] * 89)           # not enough history
    assert scoring.should_delist([0.64] * 90)               # confidently wrong for 90 → delist
    assert not scoring.should_delist([0.25] * 90)           # coin flips → no delist

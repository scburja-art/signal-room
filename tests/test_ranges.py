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


def test_competitors_raise_the_bar_by_hand():
    # 12 right out of 12, each its own try. Alone: bankroll = mean over stakes of (1+c)^12
    # = (3.14 + 8.92 + 23.3 + 56.7 + 129.7) / 5 = 44.36 → p = 1/44.36.
    briers, results = [0.04] * 12, ["right"] * 12
    alone = scoring.record(briers, results, [])
    assert alone.p_luck == pytest.approx(1 / 44.358, rel=1e-3) and alone.competitors == 1
    four = scoring.record(briers, results, [], competitors=4)
    assert four.p_luck_alone == alone.p_luck and four.p_luck == pytest.approx(4 * alone.p_luck, rel=1e-4)
    assert four.competitors == 4
    # Ranges face the same raised bar: wider with competitors.
    assert four.hit_rate_low < alone.hit_rate_low
    # 28 right of 32 alone is Proven (p ≈ 1/1,475, under 1/1,000); among 10 competitors the same record
    # is 1/148: Probably not luck. 24 of 32 (1/34 alone) drops from Probably not luck to Luck among 10.
    briers, results = [0.04] * 28 + [0.36] * 4, ["right"] * 28 + ["wrong"] * 4
    big, ten = scoring.record(briers, results, []), scoring.record(briers, results, [], competitors=10)
    assert big.verdict == "proven" and ten.verdict == "probably_not_luck"
    assert ten.p_luck == pytest.approx(10 * big.p_luck, rel=1e-3)
    briers, results = [0.04] * 24 + [0.36] * 8, ["right"] * 24 + ["wrong"] * 8
    assert scoring.record(briers, results, []).verdict == "probably_not_luck"
    assert scoring.record(briers, results, [], competitors=10).verdict == "luck"
    # A coin-flip record is not made worse than "no evidence" by the correction (capped at 1).
    flip = scoring.record([0.25] * 30, ["right", "wrong"] * 15, [], competitors=50)
    assert flip.p_luck == 1.0


def test_board_size_counts_ranked_active_agents(client, session_factory):
    from signal_room import service
    from signal_room.db import Agent as AgentRow
    from tests.api_helpers import Agent
    from tests.test_delisting import post_and_resolve
    from tests.conftest import spread
    a, b, c = Agent(client, "a"), Agent(client, "b"), Agent(client, "c")
    post_and_resolve(client, session_factory, a, 30, right=True)
    post_and_resolve(client, session_factory, b, 30, right=True)
    post_and_resolve(client, session_factory, c, 5, right=True)       # under 30: not on the board
    spread(session_factory)
    db = session_factory()
    assert service.board_size(db, service._since("90d")) == 2
    rec = a.get(f"/agents/{a.id}").json()
    assert rec["competitors"] == 2 and rec["p_luck"] == pytest.approx(min(1.0, 2 * rec["p_luck_alone"]), rel=1e-5)
    # A delisted agent leaves the field.
    db.query(AgentRow).filter_by(id=b.id).first().status = "delisted"
    db.commit()
    assert service.board_size(db, service._since("90d")) == 1

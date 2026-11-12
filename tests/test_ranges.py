"""Ranges that hold however often they are checked (approved by Steven 2026-11-04). Hand-computed."""
import math

import pytest

from signal_room import scoring

MEAN_STAKE = sum(scoring.STAKES) / len(scoring.STAKES)   # 0.225


def wealth(vals, m, side):
    """The account betting "the true mean is above m" (side +1) or below it (-1), calls one after another."""
    xs = [(v - m) / m if side > 0 else (m - v) / (1 - m) for v in vals]
    return math.exp(scoring._log_mean(scoring.Timeline.in_order(len(vals)).log_cashes(xs)))


def test_one_right_try_by_hand():
    # One call, right (value 1). The "above m" bankroll is the mean over stakes c of 1 + c (1 - m)/m
    # = 1 + 0.225 (1 - m)/m. It reaches 2/0.05 = 40 when (1 - m)/m = 39 / 0.225, i.e. m = 0.225 / 39.225.
    lo, hi = scoring.confidence_range([1.0], None)
    assert lo == pytest.approx(MEAN_STAKE / (39 + MEAN_STAKE), rel=1e-5)
    assert hi == pytest.approx(1.0, abs=1e-8)     # the "below m" bankroll is 1 - 0.225: never rules anything out


def test_mirror_image():
    vals = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 0.0, 1.0, 1.0, 0.0]
    lo, hi = scoring.confidence_range(vals, None)
    flo, fhi = scoring.confidence_range([1 - v for v in vals], None)
    assert flo == pytest.approx(1 - hi, abs=1e-9) and fhi == pytest.approx(1 - lo, abs=1e-9)


def test_edges_sit_exactly_on_the_bar():
    vals = [1.0] * 14 + [0.0] * 6
    lo, hi = scoring.confidence_range(vals, None)
    assert wealth(vals, lo, +1) == pytest.approx(40.0, rel=1e-4)
    assert wealth(vals, hi, -1) == pytest.approx(40.0, rel=1e-4)
    assert lo < 0.7 < hi
    # Hand check of the bankroll formula at one point: m = 0.5, value 1 multiplies by 1 + 2c(0.5), value 0 by 1 - c.
    by_hand = sum((1 + c) ** 14 * (1 - c) ** 6 for c in scoring.STAKES) / len(scoring.STAKES)
    assert wealth(vals, 0.5, +1) == pytest.approx(by_hand)


def test_same_bankroll_as_the_luck_odds_at_a_coin_flip():
    hits = [1] * 30 + [0] * 12
    rec = scoring.record([0.16 if h else 0.36 for h in hits], ["right" if h else "wrong" for h in hits], [])
    assert wealth([float(h) for h in hits], 0.5, +1) == pytest.approx(1 / rec.p_luck, rel=1e-4)
    # So the range clears 50% exactly when the luck odds pass 1 in 40 (each side of the range gets half of 5%).
    assert (rec.hit_rate_low > 0.5) == (rec.p_luck < 1 / 40)


def test_four_calls_open_together_by_hand():
    # Four right calls open at once. The "above m" account turns 1 into 1 + ((1 - m)/m) (1 - (1-c)^4):
    # the four stakes add up to 1 - (1-c)^4 and each comes back 1/m-fold. It reaches 40 at the low end.
    tl = scoring.Timeline([(0, 15)] * 4)
    lo, hi = scoring.confidence_range([1.0] * 4, tl)
    share = sum(1 - (1 - c) ** 4 for c in scoring.STAKES) / len(scoring.STAKES)
    assert lo == pytest.approx(share / (39 + share), rel=1e-4) and hi == pytest.approx(1.0, abs=1e-8)
    # Less sure than four right calls in a row, surer than one.
    assert scoring.confidence_range([1.0], None)[0] < lo < scoring.confidence_range([1.0] * 4, None)[0]


def test_the_range_always_holds_what_was_seen():
    import random
    rng = random.Random(2)
    for _ in range(40):
        n = rng.randrange(1, 60)
        vals = [rng.choice([0.0, 1.0, None]) for _ in range(n)]
        windows = [(t, t + rng.randrange(1, 20)) for t in sorted(rng.randrange(0, 100) for _ in range(n))]
        lo, hi = scoring.confidence_range(vals, scoring.Timeline(windows))
        scored = [v for v in vals if v is not None]
        if scored:
            assert lo <= sum(scored) / len(scored) <= hi


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
    # 12 right out of 12, one after another. Alone: bankroll = mean over stakes of (1+c)^12
    # = (1.34 + 1.80 + 3.14 + 8.92 + 23.3 + 56.7 + 129.7) / 7 = 32.13 → p = 1/32.13.
    briers, results = [0.04] * 12, ["right"] * 12
    alone = scoring.record(briers, results, [])
    assert alone.p_luck == pytest.approx(1 / 32.133, rel=1e-3) and alone.competitors == 1
    four = scoring.record(briers, results, [], competitors=4)
    assert four.p_luck_alone == alone.p_luck and four.p_luck == pytest.approx(4 * alone.p_luck, rel=1e-4)
    assert four.competitors == 4
    # Ranges face the same raised bar: wider with competitors.
    assert four.hit_rate_low < alone.hit_rate_low
    # 28 right of 32 alone is Proven (p ≈ 1/1,054, under 1/1,000); among 10 competitors the same record
    # is 1/105: Probably not luck. 24 of 32 (1/25 alone) drops from Probably not luck to Luck among 10.
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


def test_board_size_counts_every_agent_ever_scored(client, session_factory):
    from signal_room import service
    from signal_room.db import Agent as AgentRow
    from tests.api_helpers import Agent
    from tests.test_delisting import post_and_resolve
    from tests.conftest import spread
    a, b, c = Agent(client, "a"), Agent(client, "b"), Agent(client, "c")
    post_and_resolve(client, session_factory, a, 30, right=True)
    post_and_resolve(client, session_factory, b, 30, right=True)
    post_and_resolve(client, session_factory, c, 5, right=True)       # under 30, unranked, and it counts all the same
    spread(session_factory)
    db = session_factory()
    assert service.board_size(db) == 3
    rec = a.get(f"/agents/{a.id}").json()
    # By hand: three agents have been scored here, so the bar is 3 x 4 = 12.
    assert rec["competitors"] == 12 and rec["p_luck"] == pytest.approx(min(1.0, 12 * rec["p_luck_alone"]), rel=1e-5)
    # A delisted agent still counts (since 2026-11-11; until then it left the field, which let an owner
    # run many wallets, keep the luckiest and have the rest thrown out).
    db.query(AgentRow).filter_by(id=b.id).first().status = "delisted"
    db.commit()
    assert service.board_size(db) == 3

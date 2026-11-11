"""Rule set 4: every call is judged against its bar (what it cost).

Three things are pinned here: the worked examples from the approved proposal, to the cent; that a
record of plain calls gets exactly the numbers rule set 3 gave it; and that a caller with no edge
over its prices is almost never labelled, however often it is checked.
"""
import dataclasses
import math
import random

import pytest

from signal_room import scoring
from tests.frozen import scoring_rule_set_3 as old


def bankroll(payoffs, stake=0.1, start=100.0):
    """The imaginary bettor: stakes a fixed share of the bankroll on each try's payoff."""
    for x in payoffs:
        start *= 1 + stake * x
    return start


# ---------- the worked examples, by hand ----------

def test_example_1_and_2_a_priced_call_that_wins_or_loses():
    # NO at 85c; fee 7% x 0.85 x 0.15 = 0.89c, rounded up to 1c; the cost, and so the bar, is 86c.
    up, _ = scoring.call_payoffs([1, 0], [0.86, 0.86], [0.82, 0.82])
    assert up[0] == pytest.approx(0.14 / 0.86)                 # $10 buys 11.63 contracts, which pay $11.63
    assert round(bankroll([up[0]]), 2) == 101.63
    assert up[1] == -1.0 and round(bankroll([up[1]]), 2) == 90.00


def test_example_3_winning_exactly_as_often_as_the_cost_breaks_even():
    up, _ = scoring.call_payoffs([1, 0], [0.86, 0.86], [0.82, 0.82])
    assert 0.86 * up[0] + 0.14 * up[1] == pytest.approx(0.0, abs=1e-12)


def test_example_4_a_plain_call_is_an_even_bet_as_before():
    up, down = scoring.call_payoffs([1, 0])
    assert up == [1.0, -1.0] and down == [-1.0, 1.0]
    assert bankroll([1.0]) == pytest.approx(110.0) and bankroll([-1.0]) == pytest.approx(90.0)


def test_example_5_two_calls_in_one_window_are_one_try():
    # BTC NO at a cost of 80c wins, ETH NO at a cost of 70c loses; the $10 stake is split $5 each.
    up, _ = scoring.call_payoffs([1, 0], [0.80, 0.70], [0.76, 0.66])
    one_try = scoring._batch_means(up, ["w", "w"])
    assert one_try == [pytest.approx((0.25 - 1.0) / 2)]
    assert round(bankroll(one_try), 2) == 96.25


def test_example_8_claiming_an_edge_you_do_not_have_costs_credits():
    win = scoring.credit_for(scoring.brier(0.90, 1.0), "right", bar=0.86)
    loss = scoring.credit_for(scoring.brier(0.90, 0.0), "wrong", bar=0.86)
    assert win == pytest.approx(0.0384) and loss == pytest.approx(-0.2816)
    assert 0.90 * win + 0.10 * loss == pytest.approx(0.0064)      # truly right 90% of the time
    assert 0.86 * win + 0.14 * loss == pytest.approx(-0.0064)     # only as right as the price said


def test_example_9_the_favourites_hole_is_closed():
    # Cost 95c, says 95%: nothing claimed beyond the price, so nothing earned either way.
    assert scoring.credit_for(scoring.brier(0.95, 1.0), "right", bar=0.95) == pytest.approx(0.0, abs=1e-12)
    assert scoring.credit_for(scoring.brier(0.95, 0.0), "wrong", bar=0.95) == pytest.approx(0.0, abs=1e-12)
    # Under rule set 3 the same bot averaged +0.81 credits a call for free.
    free = 0.95 * old.credit_for(old.brier(0.95, 1.0), "right") + 0.05 * old.credit_for(old.brier(0.95, 0.0), "wrong")
    assert free == pytest.approx(0.81)
    # A whole record of it: no credits, no skill, however many it wins.
    hits = [1] * 95 + [0] * 5
    rec = scoring.record([scoring.brier(0.95, float(h)) for h in hits], ["right" if h else "wrong" for h in hits], [],
                         bars=[0.95] * 100, fades=[0.93] * 100)
    assert rec.bss == 0.0 and rec.score == 0.0 and rec.skill_points == 0 and rec.priced == 100


def test_example_10_a_plain_call_earns_what_it_always_did():
    assert scoring.credit_for(scoring.brier(0.6, 1.0), "right") == pytest.approx(0.36)
    assert scoring.credit_for(scoring.brier(0.6, 1.0), "right") == old.credit_for(old.brier(0.6, 1.0), "right")


def test_only_right_and_wrong_calls_earn_credits():
    for result in ("flat", "void", "unfilled"):
        assert scoring.credit_for(0.04, result, bar=0.7) == 0.0
    assert scoring.credit_for(None, "right", bar=0.7) == 0.0


# ---------- plain calls: not one digit moves ----------

def random_plain_record(rng):
    n = rng.randrange(0, 260)
    p_true = rng.choice([0.4, 0.5, 0.55, 0.62, 0.8])
    results, briers, clusters, c = [], [], [], 0
    for _ in range(n):
        if rng.random() < 0.7:
            c += 1
        clusters.append(c)
        conf = rng.choice([0.5, 0.55, 0.6, 0.73, 0.9, 1.0])
        if rng.random() < 0.08:
            results.append("flat")
            briers.append(None)
            continue
        hit = rng.random() < p_true
        results.append("right" if hit else "wrong")
        briers.append(round((conf - (1.0 if hit else 0.0)) ** 2, 6))
    returns = [rng.uniform(-0.01, 0.01) for r in results if r != "flat"]
    longs = [rng.uniform(-0.01, 0.01) for _ in returns]
    return briers, results, returns, clusters, longs


@pytest.mark.parametrize("seed", range(40))
def test_a_record_of_plain_calls_is_unchanged_to_the_last_digit(seed):
    rng = random.Random(seed)
    briers, results, returns, clusters, longs = random_plain_record(rng)
    slices, field = rng.choice([1, 1, 4]), rng.choice([1, 4, 9])
    was = dataclasses.asdict(old.record(briers, results, returns, clusters if seed % 3 else None, slices, longs, field))
    now = dataclasses.asdict(scoring.record(briers, results, returns, clusters if seed % 3 else None, slices, longs, field))
    for key, value in was.items():
        assert now[key] == value, key
    # The new fields say the same thing in the new words.
    assert now["bar"] == 0.5 and now["priced"] == 0
    if now["n"]:
        assert now["edge"] == pytest.approx(now["hit_rate"] - 0.5, abs=1e-4)
        assert now["edge_low"] == pytest.approx(now["hit_rate_low"] - 0.5, abs=1e-4)
        assert now["edge_high"] == pytest.approx(now["hit_rate_high"] - 0.5, abs=1e-4)


@pytest.mark.parametrize("seed", range(10))
def test_plain_helpers_match_rule_set_3_exactly(seed):
    rng = random.Random(100 + seed)
    batches = [(rng.randrange(0, s + 1), s) for s in (rng.randrange(1, 5) for _ in range(rng.randrange(1, 120)))]
    assert scoring.luck_odds(batches) == old.luck_odds(batches)
    assert scoring.tries_to_reach(batches, 0.05) == old.tries_to_reach(batches, 0.05)
    briers = [round(rng.random(), 6) for _ in range(rng.randrange(1, 150))]
    assert scoring.skill_score(briers) == old.skill_score(briers)
    assert scoring.should_delist(briers) == old.should_delist(briers)
    for b in briers[:20]:
        for result in ("right", "wrong", "flat"):
            assert scoring.credit_for(b, result) == old.credit_for(b, result)


def test_the_general_formulas_at_an_even_bar_are_the_plain_ones():
    """Passing bars of 0.5 explicitly (the priced code path) lands on the same luck odds and skill."""
    rng = random.Random(7)
    hits = [1 if rng.random() < 0.63 else 0 for _ in range(300)]
    briers = [scoring.brier(0.6, float(h)) for h in hits]
    results = ["right" if h else "wrong" for h in hits]
    cl = [i // 2 for i in range(300)]
    plain = scoring.record(briers, results, [], cl)
    priced = scoring.record(briers, results, [], cl, bars=[0.5] * 300, fades=[0.5] * 300)
    assert plain.p_luck == priced.p_luck < 0.05 and plain.p_worse == priced.p_worse
    assert plain.bss == priced.bss and plain.verdict == priced.verdict and plain.tries_to_next == priced.tries_to_next
    assert priced.edge_low == pytest.approx(plain.hit_rate_low - 0.5, abs=2e-4)
    assert priced.edge_high == pytest.approx(plain.hit_rate_high - 0.5, abs=2e-4)
    assert priced.bss_low == pytest.approx(plain.bss_low, abs=0.25) and priced.bss_low <= plain.bss_low + 1e-9   # wider known limits, wider range


# ---------- priced calls: fair however often checked ----------

def fee(a: float) -> float:
    return math.ceil(7 * a * (1 - a) - 1e-9) / 100


def ever_labelled(seq, threshold=0.05, side="up"):
    """seq of (hit, bar, fade). True if the luck odds ever dip under the threshold, checked after every call."""
    logs = [0.0] * len(scoring.STAKES)
    for h, r, f in seq:
        x = (h - r) / r if side == "up" else (f - h) / (1 - f)
        logs = [v + math.log1p(lam * x) for v, lam in zip(logs, scoring.STAKES)]
        top = max(logs)
        if math.exp(-(top + math.log(sum(math.exp(v - top) for v in logs) / len(logs)))) < threshold:
            return True
    return False


@pytest.mark.parametrize("lo,hi", [(0.30, 0.90), (0.85, 0.95), (0.05, 0.15)])
def test_no_edge_at_any_mix_of_prices_is_labelled_under_5_percent_of_the_time(lo, hi):
    """The hardest case for the test: the bar is exactly the true chance (no spread, no fee)."""
    rng = random.Random(int(lo * 100))
    sims, bad = 600, 0
    for _ in range(sims):
        seq = []
        for _ in range(500):
            r = rng.uniform(lo, hi)
            seq.append((1 if rng.random() < r else 0, r, r))
        bad += ever_labelled(seq)
    assert bad / sims < 0.05


def test_paying_the_real_spread_and_fee_is_labelled_on_neither_side():
    rng = random.Random(3)
    sims, good, worse = 400, 0, 0
    for _ in range(sims):
        seq = []
        for _ in range(500):
            mid = rng.uniform(0.3, 0.9)
            ask, bid = mid + 0.005, mid - 0.005
            hit = 1 if rng.random() < mid else 0
            seq.append((hit, ask + fee(ask), 1 - ((1 - bid) + fee(1 - bid))))
        good += ever_labelled(seq)
        worse += ever_labelled(seq, side="down")
    assert good / sims < 0.05 and worse / sims < 0.05


def test_a_real_edge_over_the_price_is_found_and_a_missing_one_is_not():
    def rec(p_true, cost, n=1500, seed=1):
        rng = random.Random(seed)
        hits = [1 if rng.random() < p_true else 0 for _ in range(n)]
        return scoring.record([scoring.brier(min(0.99, cost + 0.05), float(h)) for h in hits],
                              ["right" if h else "wrong" for h in hits], [], bars=[cost] * n, fades=[cost - 0.03] * n)
    skilled, none, bad = rec(0.80, 0.72), rec(0.72, 0.72), rec(0.60, 0.72)
    assert skilled.verdict in ("probably_not_luck", "proven") and skilled.edge_low > 0
    assert none.verdict == "luck" and none.edge_low < 0 < none.edge_high
    assert bad.verdict == "worse_than_chance" and bad.edge_high < 0
    assert skilled.edge == pytest.approx(skilled.hit_rate - 0.72, abs=1e-4) and skilled.bar == 0.72
    # Skill is the mean credit, and five real points of edge honestly stated are worth 4 x 0.05^2 = 0.01.
    assert skilled.bss == pytest.approx(4 * (0.08 ** 2 - 0.03 ** 2), abs=0.02)


def test_edge_range_holds_its_ground():
    rng = random.Random(11)
    bars = [rng.uniform(0.5, 0.9) for _ in range(400)]
    fades = [b - 0.03 for b in bars]
    hits = [1 if rng.random() < b + 0.06 else 0 for b in bars]
    lo, hi = scoring.edge_range(hits, bars, fades, None)
    observed = sum(hits) / 400 - sum(bars) / 400
    assert lo < observed < hi and lo > -0.15 and hi < 0.25
    # Calls made together count once: every call repeated four times inside its own try changes nothing.
    four = [i for i in range(400) for _ in range(4)]
    again = scoring.edge_range([hits[i] for i in four], [bars[i] for i in four], [fades[i] for i in four], four)
    assert again == pytest.approx((lo, hi), abs=1e-9)
    assert scoring.edge_range([], [], [], None) == (-1.0, 1.0)


def test_mixed_record_counts_each_call_against_its_own_bar():
    # 30 plain calls right half the time (no edge) and 30 priced calls that win 29 of 30 at a 60c cost.
    briers = [0.25] * 30 + [scoring.brier(0.7, 1.0)] * 29 + [scoring.brier(0.7, 0.0)]
    results = ["right", "wrong"] * 15 + ["right"] * 29 + ["wrong"]
    rec = scoring.record(briers, results, [], bars=[None] * 30 + [0.6] * 30, fades=[None] * 30 + [0.57] * 30)
    assert rec.priced == 30 and rec.n == 60
    assert rec.bar == pytest.approx(0.55) and rec.edge == pytest.approx(rec.hit_rate - 0.55, abs=1e-4)
    plain_only = scoring.record(briers[:30], results[:30], [])
    assert plain_only.p_luck == 1.0 and rec.p_luck < 0.05


def test_delisting_uses_the_bar():
    # 90 calls at a 90c cost, stated 95%, winning 90%: honest about the price, a little overclaimed.
    hits = [1] * 81 + [0] * 9
    briers = [scoring.brier(0.95, float(h)) for h in hits]
    refs = [scoring.ref_brier(0.90, float(h)) for h in hits]
    assert not scoring.should_delist(briers, refs)
    # Against a coin-flip bar the same record looks brilliant; against its own bar it is about zero.
    assert scoring.skill_score(briers) > 0.5 and abs(scoring.skill_score(briers, refs)) < 0.05
    # Stating 99% on 60c contracts that win 60%: badly overclaimed, delisted.
    hits = [1] * 54 + [0] * 36
    briers = [scoring.brier(0.99, float(h)) for h in hits]
    assert scoring.should_delist(briers, [scoring.ref_brier(0.60, float(h)) for h in hits])

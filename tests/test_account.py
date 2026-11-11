"""The betting account behind the luck odds and the ranges (approved by Steven 2026-11-11).

A stake is a fixed fraction of the cash that is free when a call starts; it comes back, with its
winnings, when the call closes; only cash in hand counts. Every expected value here is worked by hand.
The simulations replay the trick an independent review found on 2026-11-11: under the rule before
this one, a bot with no edge could shrink its losing tries by adding calls to them.
"""
import math
import random

import pytest

from signal_room import scoring
from signal_room.scoring import Timeline
from tests.frozen import scoring_rule_set_3 as old


def cash(windows, payoffs, stake, pending=None):
    return math.exp(Timeline(windows, pending).log_cash(payoffs, stake))


# ---------- by hand ----------

def test_calls_one_after_another_are_a_running_product():
    # 10% staked each time: win, lose, win at even bars → 1.1 x 0.9 x 1.1.
    assert cash([(0, 1), (1, 2), (2, 3)], [1, -1, 1], 0.1) == pytest.approx(1.1 * 0.9 * 1.1)
    assert math.exp(Timeline.in_order(3).log_cash([1, -1, 1], 0.1)) == pytest.approx(1.089)


def test_two_calls_open_at_once():
    # 50% stakes. A takes 0.50 of 1.00; B, starting while A is open, takes half of the 0.50 left.
    # A wins at an even bar (0.50 comes back as 1.00), B loses: 0.25 + 1.00 = 1.25.
    assert cash([(0, 10), (5, 15)], [1, -1], 0.5) == pytest.approx(1.25)
    # The other way round: A loses, B wins: 0.25 + 0.50 = 0.75.
    assert cash([(0, 10), (5, 15)], [-1, 1], 0.5) == pytest.approx(0.75)


def test_example_5_two_priced_calls_in_one_window():
    # $100 at 10% stakes. BTC NO costs 80c: $10 staked. ETH NO costs 70c, posted while BTC is open: 10% of
    # the $90 left, $9. BTC wins: $10 buys 12.5 contracts that pay $12.50. ETH loses. $81 + $12.50 = $93.50.
    up, _ = scoring.call_payoffs([1, 0], [0.80, 0.70], [0.76, 0.66])
    assert 100 * cash([(0, 15), (1, 15)], up, 0.1) == pytest.approx(93.50)


def test_a_flat_call_gets_its_stake_back():
    # A stakes 0.50, B stakes 0.25. A is flat (0.50 back), B wins (0.25 back as 0.50): 0.25 + 0.50 + 0.50.
    assert cash([(0, 10), (5, 15)], [0, 1], 0.5) == pytest.approx(1.25)


def test_money_on_open_calls_counts_for_nothing():
    # A stakes 0.50, B 0.25, and a call still open takes 0.125 of what is left. A wins, B loses:
    # cash in hand is 0.125 + 1.00 = 1.125, not the 1.25 it would be without the open call.
    assert cash([(0, 10), (5, 15)], [1, -1], 0.5, pending=[7]) == pytest.approx(1.125)
    assert cash([], [], 0.5, pending=[0, 1]) == pytest.approx(0.25)


def test_a_call_that_closes_as_another_starts_is_paid_first():
    # A closes at 10 and B starts at 10: B's stake is half of what A returned, as if one followed the other.
    assert cash([(0, 10), (10, 20)], [1, 1], 0.5) == pytest.approx(1.5 * 1.5)


def test_calls_started_at_the_same_instant_are_taken_in_order():
    # Four at once, all win at even bars, 50% stakes: stakes 1/2, 1/4, 1/8, 1/16 each come back doubled.
    assert cash([(0, 15)] * 4, [1] * 4, 0.5) == pytest.approx(1 / 16 + 2 * (15 / 16))
    # In general: 2 - (1 - stake)^4.
    assert cash([(0, 15)] * 4, [1] * 4, 0.1) == pytest.approx(2 - 0.9 ** 4)


def test_a_priced_call_pays_at_its_own_odds():
    # Cost 73c, 20% staked, wins: 0.20 buys 0.20 / 0.73 contracts, each paying 1.
    up, down = scoring.call_payoffs([1], [0.73], [0.68])
    assert math.exp(Timeline.in_order(1).log_cash(up, 0.2)) == pytest.approx(0.8 + 0.2 / 0.73)
    # Betting against it (the other side cost 1 - 0.68 = 32c) loses the stake.
    assert math.exp(Timeline.in_order(1).log_cash(down, 0.2)) == pytest.approx(0.8)


def test_luck_odds_average_the_seven_stakes():
    assert scoring.STAKES == (0.025, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5)
    # One right call: the mean of 1 + stake is 1.225. One wrong: every account is down, no evidence.
    assert scoring.luck_from([1.0], [-1.0]) == pytest.approx((1 / 1.225, 1.0))
    assert scoring.luck_from([-1.0], [1.0]) == pytest.approx((1.0, 1 / 1.225))
    assert scoring.luck_from([], []) == (1.0, 1.0)


def test_long_records_stay_in_range():
    n = 20000
    assert Timeline.in_order(n).log_cash([-1.0] * n, 0.5) == pytest.approx(n * math.log(0.5))
    assert Timeline.in_order(n).log_cash([1.0] * n, 0.5) == pytest.approx(n * math.log(1.5))
    # 400 calls open at once that all pay a fortune: no overflow, and the hand value holds
    # (the stakes add up to all but 0.5^400 of the cash, each back 1e10-fold).
    assert Timeline([(0, 1)] * 400).log_cash([1e10 - 1] * 400, 0.5) == pytest.approx(math.log(1e10))


def test_more_winnings_never_mean_less_cash():
    rng = random.Random(4)
    windows = [(t, t + rng.randrange(1, 30)) for t in sorted(rng.randrange(0, 200) for _ in range(60))]
    xs = [rng.choice([-1.0, 0.0, 0.5, 1.0]) for _ in windows]
    tl = Timeline(windows)
    base = tl.log_cash(xs, 0.3)
    for i in range(len(xs)):
        better = xs[:i] + [xs[i] + 0.25] + xs[i + 1:]
        assert tl.log_cash(better, 0.3) >= base - 1e-12


# ---------- continuity with the rule before ----------

def test_for_calls_that_never_overlap_each_stake_is_the_old_running_product():
    rng = random.Random(9)
    xs = [rng.choice([1.0, -1.0]) for _ in range(300)]
    for stake in (0.1, 0.2, 0.3, 0.4, 0.5):
        assert Timeline.in_order(300).log_cash(xs, stake) == pytest.approx(sum(math.log1p(stake * x) for x in xs), abs=1e-9)


def test_with_the_old_five_stakes_a_record_that_never_overlaps_gets_the_old_odds(monkeypatch):
    monkeypatch.setattr(scoring, "STAKES", (0.1, 0.2, 0.3, 0.4, 0.5))
    rng = random.Random(10)
    hits = [1 if rng.random() < 0.6 else 0 for _ in range(200)]
    up, down = scoring.call_payoffs(hits)
    assert scoring.luck_from(up, down) == pytest.approx(old.luck_odds([(h, 1) for h in hits]), rel=1e-9)


# ---------- the review's trick, and why the account is immune ----------

class Book:
    """The same account kept by hand as the calls happen, so a simulation can look after every close."""

    def __init__(self):
        self.cash = [1.0] * len(scoring.STAKES)
        self.held = {}

    def open(self, key):
        self.held[key] = [s * c for s, c in zip(scoring.STAKES, self.cash)]
        self.cash = [c - h for c, h in zip(self.cash, self.held[key])]

    def close(self, key, payoff):
        self.cash = [c + h * (1 + payoff) for c, h in zip(self.cash, self.held.pop(key))]

    def odds(self):
        return min(1.0, len(self.cash) / sum(self.cash))


def padding_bot(rng, windows, priced):
    """No edge anywhere. One fair call at the start of each window. If it is losing late on, three more
    calls are added: 96c favourites that win 96% of the time (priced), or fair coin flips (plain).
    Yields (start, end, payoff) in start order."""
    t = 0.0
    for _ in range(windows):
        win = rng.random() < 0.5
        yield (t + 1, t + 15, 1.0 if win else -1.0)
        if not win:
            for k in range(3):
                if priced:
                    yield (t + 12 + k / 10, t + 15, 0.04 / 0.96 if rng.random() < 0.96 else -1.0)
                else:
                    yield (t + 13 + k / 10, t + 28, 1.0 if rng.random() < 0.5 else -1.0)
        t += 15 if priced else 30


def ever_labelled_old(calls, level=0.05):
    """The rule before the account: calls that share market time are one try, the try pays their mean."""
    ids = scoring.cluster_ids([(a, b) for a, b, _ in calls])
    tries: dict = {}
    for i, (_, _, x) in zip(ids, calls):
        tries.setdefault(i, []).append(x)
    logs = [0.0] * 5
    for xs in tries.values():
        m = sum(xs) / len(xs)
        logs = [v + math.log1p(s * m) for v, s in zip(logs, (0.1, 0.2, 0.3, 0.4, 0.5))]
        if 5 / sum(math.exp(v) for v in logs) < level:
            return True
    return False


def ever_labelled(calls, level=0.05):
    """The account, looked at after every single close."""
    book, events = Book(), []
    for i, (a, b, x) in enumerate(calls):
        events += [(a, 1, i, x), (b, 0, i, x)]
    for _, is_open, i, x in sorted(events):
        if is_open:
            book.open(i)
        else:
            book.close(i, x)
            if book.odds() < level:
                return True
    return False


def test_the_book_in_these_tests_is_the_room_s_account():
    rng = random.Random(21)
    for priced in (True, False):
        calls = list(padding_bot(rng, 40, priced))
        book = Book()
        for _, is_open, i, x in sorted(e for i, (a, b, x) in enumerate(calls) for e in ((a, 1, i, x), (b, 0, i, x))):
            book.open(i) if is_open else book.close(i, x)
        xs = [x for _, _, x in calls]
        tl = Timeline([(a, b) for a, b, _ in calls])
        assert book.odds() == pytest.approx(min(1.0, math.exp(-scoring._log_mean(tl.log_cashes(xs)))), rel=1e-9)


@pytest.mark.parametrize("priced", [True, False])
def test_padding_losing_tries_no_longer_certifies_a_bot_with_no_edge(priced):
    rng = random.Random(1111 + priced)
    runs, old_rule, account = 400, 0, 0
    for _ in range(runs):
        calls = list(padding_bot(rng, 200, priced))
        old_rule += ever_labelled_old(calls)
        account += ever_labelled(calls)
    assert old_rule / runs > 0.9           # the finding: nearly every such bot was certified
    assert account / runs <= 0.05          # the promise: at most 1 in 20, however often it is checked


def test_the_padding_record_through_the_real_scoring_code():
    """One padded record, scored whole: the account sees the losses at full size."""
    rng = random.Random(5)
    calls = list(padding_bot(rng, 150, priced=False))
    hits = [1 if x > 0 else 0 for _, _, x in calls]
    windows = [(a, b) for a, b, _ in calls]
    rec = scoring.record([0.25] * len(calls), ["right" if h else "wrong" for h in hits], [],
                         scoring.cluster_ids(windows), windows=windows)
    was = old.record([0.25] * len(calls), ["right" if h else "wrong" for h in hits], [], old.cluster_ids(windows))
    assert was.verdict in ("probably_not_luck", "proven")      # what the room would have said
    assert rec.verdict == "luck" and rec.p_luck > 0.05         # what it says now
    assert rec.tries == was.tries                              # tries are still counted the same way


def test_calls_that_overlap_at_their_edges_no_longer_inflate_the_odds():
    """The review's second finding: two coins at minute 0 and two at minute 14 of every 15-minute window.
    The late pair shares one minute with its own try and fourteen with the next."""
    rng = random.Random(77)
    runs, account = 400, 0
    for _ in range(runs):
        calls, t = [], 0.0
        for _ in range(250):
            early = rng.random() < 0.5
            late = rng.random() < 0.5
            calls += [(t, t + 15, 1.0 if early else -1.0)] * 2 + [(t + 14, t + 29, 1.0 if late else -1.0)] * 2
            t += 15
        account += ever_labelled(calls)
    assert account / runs <= 0.05


def test_an_honest_bot_is_still_found(monkeypatch):
    """Right 58% of the time, four coins a window that mostly move together: certified, and about as
    fast as one coin a window (the two smallest stakes are there for exactly this)."""
    rng = random.Random(8)

    def windows_needed(coins):
        book, t = Book(), 0
        for w in range(4000):
            shared = rng.random() < 0.58
            for k in range(coins):
                book.open((w, k))
            for k in range(coins):
                hit = shared if rng.random() < 0.8 else rng.random() < 0.58
                book.close((w, k), 1.0 if hit else -1.0)
            if book.odds() < 0.05 / 4:
                return w + 1
        return None
    one = sorted(windows_needed(1) or 9999 for _ in range(40))[20]
    four = sorted(windows_needed(4) or 9999 for _ in range(40))[20]
    assert one < 1200 and four < 1200


def test_calls_starting_together_are_staked_in_the_order_they_were_posted_whichever_closes_first():
    """Two calls start at the same instant: A (posted first, a 1-hour call) and B (posted second, 15 minutes).
    B closes first. While A is still open it is 'pending' and comes after B in the lists, but its stake
    must be the one fixed when the calls were made: A first, then B from what was left."""
    # 50% stakes: A takes 0.50, B takes 0.25. B wins: cash 0.25 + 0.50 = 0.75 while A is open.
    while_open = Timeline([(0, 15)], pending=[0], order=[("p2", "b"), ("p1", "a")])
    assert math.exp(while_open.log_cash([1.0], 0.5)) == pytest.approx(0.75)
    # A then loses: nothing more comes back. The account never held anything else.
    closed = Timeline([(0, 15), (0, 60)], order=[("p2", "b"), ("p1", "a")])
    assert math.exp(closed.log_cash([1.0, -1.0], 0.5)) == pytest.approx(0.75)
    # Without the posting order the lists' own order would have staked B first (0.50) and shown 1.25.
    assert math.exp(Timeline([(0, 15)], pending=[0]).log_cash([1.0], 0.5)) == pytest.approx(1.25)


def test_record_passes_the_posting_order_through():
    briers, results = [0.25, 0.25], ["right", "wrong"]
    first = scoring.record(briers, results, [], windows=[(0, 15), (0, 15)], order=[1, 2])
    second = scoring.record(briers, results, [], windows=[(0, 15), (0, 15)], order=[2, 1])
    assert first.p_luck_alone != second.p_luck_alone or first.hit_rate_low != second.hit_rate_low

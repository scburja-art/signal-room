"""The attack suite (tests/attacks.py), held to the room's promise on every change.

A bot with no skill, checked after every single call, is labelled "probably not luck" in at most 1 life
in 20. Every attack here is seeded, so these are the same numbers on every run; a change to the scoring
that lets one of them through fails here before it reaches the room.
"""
import math
import random

import pytest

from signal_room import config, scoring, service
from signal_room.db import Agent as AgentRow
from signal_room.scoring import Timeline
from tests import attacks
from tests.api_helpers import Agent
from tests.conftest import spread
from tests.test_delisting import post_and_resolve


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 10_000)


# ---------- the suite's account is the room's account ----------

@pytest.mark.parametrize("attack", attacks.BLOCKED, ids=lambda a: a.name)
def test_the_hand_kept_account_is_the_room_s_account(attack):
    calls = attack.life(random.Random(attack.seed))
    book = attacks.Book()
    for _, is_open, i, x in attacks.events(calls):
        book.open(i) if is_open else book.close(i, x)
    room = Timeline([(a, b) for a, b, _ in calls]).log_cashes([x for _, _, x in calls])
    assert book.odds() == pytest.approx(min(1.0, math.exp(-scoring._log_mean(room))), rel=1e-9)


def test_a_record_s_own_luck_odds_are_the_account_s():
    """One coin-flipper's life through the real scoring code, start to finish."""
    calls = attacks.coin_flipper(random.Random(9))
    windows = [(a, b) for a, b, _ in calls]
    rec = scoring.record([0.25] * len(calls), ["right" if x > 0 else "wrong" for _, _, x in calls], [],
                         scoring.cluster_ids(windows), windows=windows)
    book = attacks.Book()
    for _, is_open, i, x in attacks.events(calls):
        book.open(i) if is_open else book.close(i, x)
    assert rec.p_luck_alone == pytest.approx(book.odds(), abs=1e-6)


def test_a_priced_payoff_is_the_call_s_own_odds_fee_included():
    # By hand: a 95c contract costs 95c + 1c fee = 96c. A win returns 4c on 96c; a loss returns nothing.
    assert attacks.priced(0.95, True) == pytest.approx(0.04 / 0.96)
    assert attacks.priced(0.95, False) == -1.0
    assert scoring.call_payoffs([1, 0], [0.96, 0.96])[0] == pytest.approx([0.04 / 0.96, -1.0])


# ---------- blocked ----------

@pytest.mark.parametrize("attack", attacks.BLOCKED, ids=lambda a: a.name)
def test_a_bot_with_no_skill_is_labelled_in_at_most_one_life_in_twenty(attack):
    assert attacks.rate(attack) <= attacks.LEVEL


@pytest.mark.parametrize("attack", attacks.BLOCKED, ids=lambda a: a.name)
def test_and_is_proven_in_at_most_one_life_in_a_hundred(attack):
    # The promise is 1 in 1,000; 400 lives can only show "none, or a rare one".
    assert attacks.rate(attack, attacks.PROVEN) <= 0.01


def test_the_best_of_twenty_coin_flippers_is_held_to_the_same_line():
    assert attacks.field_rate() <= attacks.LEVEL


def test_the_false_alarm_number():
    m = attacks.monkeys()
    assert m["labelled"] / m["agents"] <= attacks.LEVEL
    assert m["proven"] / m["agents"] <= 2 * attacks.PROVEN
    assert m["labelled_on_one_board"] == 0


def test_no_skill_earns_no_credits_whatever_it_says():
    for name, _, credit in attacks.credit_tricks():
        assert credit <= 0.005, name          # zero or worse, to within the noise of 20,000 draws


def test_the_loud_coin_flipper_is_delisted():
    # By hand: saying 99% on a coin flip scores 0.0001 when right and 0.9801 when wrong. 45 of each
    # averages 0.4901, a skill of 1 - 0.4901/0.25 = -0.96, far under the -0.10 line.
    briers = [0.0001, 0.9801] * 45
    assert scoring.skill_score(briers) == pytest.approx(-0.9604)
    assert scoring.should_delist(briers)


# ---------- one owner, many wallets ----------

def test_the_bar_by_hand():
    assert [attacks.bar(n) for n in (1, 2, 3, 5, 20, 100)] == [2, 6, 12, 30, 420, 10100]
    # The shares add up to the whole 5% and no more: 1/(1x2) + 1/(2x3) + ... + 1/(n(n+1)) = n/(n+1) < 1.
    assert sum(1 / attacks.bar(k) for k in range(1, 10001)) == pytest.approx(10000 / 10001)


@pytest.mark.parametrize("name,trick,life", attacks.MANY_WALLETS, ids=[m[0] for m in attacks.MANY_WALLETS])
def test_an_owner_with_twenty_wallets_side_by_side_is_held_to_the_line(name, trick, life):
    assert attacks.open_rate(life, attacks.WALLETS, counted=False) > 0.15       # what the trick did before
    assert attacks.open_rate(life, attacks.WALLETS, counted=True) <= attacks.LEVEL


def test_an_owner_running_wallets_one_after_another_is_held_to_the_line():
    assert attacks.one_after_another(False) > attacks.LEVEL                      # a bar of k was not enough
    assert attacks.one_after_another(True) <= attacks.LEVEL                      # k x (k + 1) is


def test_what_is_known_to_be_open_is_on_the_page():
    assert [name for name, _ in attacks.OPEN] == ["Slice views"]      # found by the review of 2026-11-12
    assert all(text in attacks.page() for _, text in attacks.OPEN)


def aged(factory, agent_id, days):
    """Move an agent's calls back in time, as if they had been made and scored that long ago."""
    import datetime as dt
    from signal_room.db import Signal
    db = factory()
    for s in db.query(Signal).filter(Signal.agent_id == agent_id).all():
        for col in ("submitted_at", "start_at", "resolve_at", "resolved_at"):
            if getattr(s, col) is not None:
                setattr(s, col, getattr(s, col) - dt.timedelta(days=days))
    db.commit()
    db.close()


def test_through_the_room_every_agent_ever_scored_counts_for_good(client, session_factory):
    """The four tricks' common root, in the room itself: one keeper and four wallets an owner would like forgotten."""
    keeper, short, quiet, thrown, gone = (Agent(client, n) for n in ("keeper", "one-short", "gone-quiet", "thrown-out", "retired"))
    post_and_resolve(client, session_factory, keeper, 30, right=True)
    post_and_resolve(client, session_factory, short, 29, right=True)          # never ranked
    for bot in (quiet, thrown, gone):
        post_and_resolve(client, session_factory, bot, 30, right=True)
    spread(session_factory)
    aged(session_factory, quiet.id, 200)                                       # silent for 200 days
    db = session_factory()
    db.query(AgentRow).filter(AgentRow.id == thrown.id).update({"status": "delisted"})
    db.query(AgentRow).filter(AgentRow.id == gone.id).update({"status": "retired"})
    db.commit()
    assert service.board_size(db) == 5 and service.field_bar(db) == 30
    rec = keeper.get(f"/agents/{keeper.id}").json()
    # By hand: five agents have been scored here, so the keeper's odds are 5 x 6 = 30 times its odds alone.
    assert rec["competitors"] == 30 and rec["p_luck"] == pytest.approx(min(1.0, 30 * rec["p_luck_alone"]), abs=2e-5)
    assert [r["agent_id"] for r in service.leaderboard(db, None, None, "90d")] == [keeper.id]   # only the keeper is listed
    db.close()


def test_a_verdict_rests_on_the_whole_record_whatever_window_is_asked_for(client, session_factory):
    """Thirty right calls 200 days ago, then four wrong ones this week. The 30- and 90-day views show the
    four; the verdict, the odds and the ranges are the whole record's in every view."""
    bot = Agent(client, "long-lived")
    post_and_resolve(client, session_factory, bot, 30, right=True)
    spread(session_factory)
    aged(session_factory, bot.id, 200)
    post_and_resolve(client, session_factory, bot, 4, right=False)
    db = session_factory()
    whole = service.agent_record(db, bot.id, None, "all")
    assert whole["n"] == 34
    for window in ("30d", "90d"):
        view = service.agent_record(db, bot.id, None, window)
        assert view["n"] == 4 and view["hit_rate"] == 0.0                      # what happened in the window
        for key in service.WHOLE_RECORD:
            assert view[key] == whole[key], (window, key)                      # what the verdict rests on
    db.close()


def test_a_yardstick_never_counts(client, session_factory, yardsticks):
    a, stick = Agent(client, "a"), Agent(client, "momentum-control")
    yardsticks("momentum.json", stick)
    for bot in (a, stick):
        post_and_resolve(client, session_factory, bot, 30, right=True)
    spread(session_factory)
    db = session_factory()
    assert service.board_size(db) == 1 and service.field_bar(db) == 2
    db.close()


# ---------- the public page ----------

def test_the_published_page_is_what_the_code_measures_today():
    assert attacks.PAGE.read_text() == attacks.page(), "run: python -m tests.attacks --write"

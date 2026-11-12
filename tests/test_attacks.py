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


# ---------- open: measured, not yet blocked ----------
# This passes because the gap is there. When a fix lands, its first assertion fails: move the attack to
# the blocked list and turn the test round, as was done for the delisted decoys on 2026-11-11.

@pytest.mark.parametrize("name,trick,life,wallets", attacks.OPEN, ids=lambda v: v if isinstance(v, str) and len(v) < 30 else "")
def test_open_gap_many_wallets_keep_the_lucky_one(name, trick, life, wallets):
    assert attacks.open_rate(life, wallets, counted=False) > attacks.LEVEL, f"{name} no longer works: move it to BLOCKED"
    assert attacks.open_rate(life, wallets, counted=True) <= attacks.LEVEL     # what counting every wallet would do


def test_open_gap_a_wallet_under_thirty_calls_is_not_counted(client, session_factory):
    ranked, short = Agent(client, "ranked"), Agent(client, "one-short")
    post_and_resolve(client, session_factory, ranked, 30, right=True)
    post_and_resolve(client, session_factory, short, 29, right=True)
    spread(session_factory)
    db = session_factory()
    assert service.board_size(db, service._since("90d")) == 1
    db.close()


def test_a_delisted_or_retired_wallet_keeps_counting_toward_the_board(client, session_factory):
    """The decoy trick, through the room itself. Three wallets, each ranked on 30 scored calls."""
    keeper, decoy, other = Agent(client, "keeper"), Agent(client, "decoy"), Agent(client, "other-decoy")
    for bot in (keeper, decoy, other):
        post_and_resolve(client, session_factory, bot, 30, right=True)
    spread(session_factory)
    db = session_factory()
    assert service.board_size(db, service._since("90d")) == 3
    before = keeper.get(f"/agents/{keeper.id}").json()
    db.query(AgentRow).filter(AgentRow.id == decoy.id).update({"status": "delisted"})
    db.query(AgentRow).filter(AgentRow.id == other.id).update({"status": "retired"})
    db.commit()
    assert service.board_size(db, service._since("90d")) == 3          # before 2026-11-11 this fell to 1
    after = keeper.get(f"/agents/{keeper.id}").json()
    # By hand: the keeper's bar is still raised 3x, so its odds are three times its odds alone.
    assert after["competitors"] == 3 and after["p_luck"] == pytest.approx(min(1.0, 3 * after["p_luck_alone"]), rel=1e-5)
    for key in ("p_luck", "p_worse", "verdict", "hit_rate_low", "hit_rate_high", "edge_low", "edge_high", "tries_to_next"):
        assert after[key] == before[key], key
    # The thrown-out wallets are off the leaderboard all the same; only the count remembers them.
    assert [r["agent_id"] for r in service.leaderboard(db, None, None, "90d")] == [keeper.id]
    db.close()


def test_the_decoy_trick_is_held_to_the_line_once_every_ranked_wallet_counts():
    assert attacks.open_rate(attacks.delisted_decoys, attacks.DECOYS, counted=False) > 0.15    # what it did before the fix
    assert attacks.open_rate(attacks.delisted_decoys, attacks.DECOYS, counted=True) <= attacks.LEVEL


def test_a_board_with_nobody_thrown_out_counts_as_it_always_did(client, session_factory, yardsticks):
    """The fix changes nothing where no wallet has been delisted: two real bots, one yardstick, one unranked."""
    a, b, short, stick = Agent(client, "a"), Agent(client, "b"), Agent(client, "short"), Agent(client, "momentum-control")
    yardsticks("momentum.json", stick)
    for bot, n in ((a, 30), (b, 30), (short, 29), (stick, 30)):
        post_and_resolve(client, session_factory, bot, n, right=True)
    spread(session_factory)
    db = session_factory()
    assert service.board_size(db, service._since("90d")) == 2
    db.query(AgentRow).filter(AgentRow.id == stick.id).update({"status": "delisted"})    # a thrown-out yardstick still never counts
    db.commit()
    assert service.board_size(db, service._since("90d")) == 2
    db.close()


# ---------- the public page ----------

def test_the_published_page_is_what_the_code_measures_today():
    assert attacks.PAGE.read_text() == attacks.page(), "run: python -m tests.attacks --write"

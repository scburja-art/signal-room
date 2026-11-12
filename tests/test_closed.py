"""An identity that was closed and carried on under another (approved by Steven 2026-11-11: Kakalshi's
plain-call record, continued as its priced one). The old record stays visible as history, takes no rank,
shows where it continues in place of a verdict, and still counts toward the size of the board, so that
changing identity is never a way to start over unseen. Known by the room's own identity files, never by name."""
import pytest

from signal_room import config, feed, report, service, status
from tests.api_helpers import Agent
from tests.conftest import spread
from tests.test_delisting import post_and_resolve

PAIR = (("old.json", "new.json"),)


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 10_000)
    monkeypatch.setattr(config, "CLOSED_STATE_FILES", PAIR)


def room(client, factory, yardsticks, link=True):
    bots = {name: Agent(client, name) for name in ("alpha", "kakalshi-old", "kakalshi-new")}
    for bot in bots.values():
        post_and_resolve(client, factory, bot, 30, right=True)
    spread(factory)
    if link:
        yardsticks("old.json", bots["kakalshi-old"])
        yardsticks("new.json", bots["kakalshi-new"])
    return bots


def test_a_closed_identity_names_its_successor_keeps_its_numbers_and_takes_no_rank(client, session_factory, yardsticks):
    bots = room(client, session_factory, yardsticks, link=False)
    old = bots["kakalshi-old"]
    before = old.get(f"/agents/{old.id}").json()
    yardsticks("old.json", old)
    yardsticks("new.json", bots["kakalshi-new"])
    after = old.get(f"/agents/{old.id}").json()
    assert before["verdict"] != "closed"
    assert after["verdict"] == "closed" and after["continued_as"] == "kakalshi-new" and after["continued_as_id"] == bots["kakalshi-new"].id
    assert after["next_verdict"] is None and after["tries_to_next"] is None and after["days_to_next"] is None
    for key in ("n", "tries", "hit_rate", "p_luck", "p_luck_alone", "p_worse", "bss", "score", "competitors", "hit_rate_low", "hit_rate_high"):
        assert after[key] == before[key], key
    db = session_factory()
    ranks = {r["name"]: r["rank"] for r in service.leaderboard(db, None, None, "90d")}
    # Three ranked records listed; the closed one holds no place, so the other two are 1 and 2.
    assert ranks["kakalshi-old"] is None and sorted(v for v in ranks.values() if v) == [1, 2]
    db.close()


def test_a_closed_identity_still_counts_toward_the_board(client, session_factory, yardsticks):
    bots = room(client, session_factory, yardsticks)
    db = session_factory()
    assert service.board_size(db, service._since("90d")) == 3
    rec = bots["alpha"].get(f"/agents/{bots['alpha'].id}").json()
    # By hand: three wallets have been scored here, the closed one among them, so the bar is 3 x 4 = 12.
    assert rec["competitors"] == 12 and rec["p_luck"] == pytest.approx(min(1.0, 12 * rec["p_luck_alone"]), rel=1e-5)
    db.close()


def test_the_successor_and_everyone_else_are_untouched(client, session_factory, yardsticks):
    bots = room(client, session_factory, yardsticks, link=False)
    before = {n: b.get(f"/agents/{b.id}").json() for n, b in bots.items() if n != "kakalshi-old"}
    yardsticks("old.json", bots["kakalshi-old"])
    yardsticks("new.json", bots["kakalshi-new"])
    for name, was in before.items():
        assert bots[name].get(f"/agents/{bots[name].id}").json() == was, name


def test_a_name_alone_closes_nothing_and_half_a_pair_closes_nothing(client, session_factory, yardsticks):
    bots = room(client, session_factory, yardsticks, link=False)
    old = bots["kakalshi-old"]
    assert old.get(f"/agents/{old.id}").json()["verdict"] != "closed"        # no identity files at all
    yardsticks("old.json", old)                                               # the old file without its successor
    assert old.get(f"/agents/{old.id}").json()["verdict"] != "closed"
    yardsticks("new.json", old)                                               # a successor that is the same wallet
    assert old.get(f"/agents/{old.id}").json()["verdict"] != "closed"


def test_how_it_reads(client, session_factory, yardsticks):
    bots = room(client, session_factory, yardsticks)
    db = session_factory()
    text = status.report(db)
    assert "closed: continued as kakalshi-new" in text and "Closed" in text
    assert "–" in [line.split()[0] for line in text.splitlines() if "kakalshi-old" in line and "scored" in line]   # no place in the ranking
    from signal_room.db import Agent as AgentRow
    assert report.verdict_before(db, db.get(AgentRow, bots["kakalshi-old"].id), service.utcnow()) == "closed"
    assert feed.VERDICTS["closed"] == "Closed"
    db.close()


def test_the_scoreboard_row(client, session_factory, yardsticks, monkeypatch):
    room(client, session_factory, yardsticks)
    monkeypatch.setattr(feed, "SessionLocal", session_factory, raising=False)
    rows = [line for line in feed.scoreboard(color=False) if "continued as" in line]
    assert len(rows) == 1 and "Closed: continued as" in rows[0] and "30 calls kept as history" in rows[0]
    assert len(rows[0]) <= 100

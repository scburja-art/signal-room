"""The room's own yardstick bots are not competitors (approved by Steven 2026-11-11): they do not count
toward the size of the board, and they are shown as a control in place of a verdict."""
import pytest

from signal_room import config, feed, service, status
from signal_room.db import Agent as AgentRow
from tests.api_helpers import Agent
from tests.conftest import spread
from tests.test_delisting import post_and_resolve


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 10_000)


def board(client, factory, names, right=True):
    bots = {name: Agent(client, name) for name in names}
    for bot in bots.values():
        post_and_resolve(client, factory, bot, 30, right=right)
    spread(factory)
    return bots


def test_yardsticks_do_not_count_toward_the_size_of_the_board(client, session_factory):
    # Two real bots and two yardsticks, each with 30 scored calls. The field a reader chooses among is two.
    bots = board(client, session_factory, ["alpha", "beta", "momentum-control", "favourite-control"])
    db = session_factory()
    assert service.board_size(db, service._since("90d")) == 2
    rec = bots["alpha"].get(f"/agents/{bots['alpha'].id}").json()
    # By hand: the bar is raised 2x, not 4x.
    assert rec["competitors"] == 2 and rec["p_luck"] == pytest.approx(min(1.0, 2 * rec["p_luck_alone"]), rel=1e-5)
    db.close()


def test_a_real_bot_s_numbers_do_not_move_when_a_yardstick_joins(client, session_factory):
    bots = board(client, session_factory, ["alpha", "beta"])
    before = bots["alpha"].get(f"/agents/{bots['alpha'].id}").json()
    late = Agent(client, "favourite-control")
    post_and_resolve(client, session_factory, late, 30, right=True)
    spread(session_factory)
    after = bots["alpha"].get(f"/agents/{bots['alpha'].id}").json()
    for key in ("competitors", "p_luck", "p_worse", "verdict", "hit_rate_low", "hit_rate_high", "tries_to_next", "score"):
        assert after[key] == before[key], key


def test_a_yardstick_is_labelled_a_control_and_keeps_its_numbers(client, session_factory):
    bots = board(client, session_factory, ["alpha", "momentum-control"])
    reader = bots["alpha"]
    control = reader.get(f"/agents/{bots['momentum-control'].id}").json()
    assert control["verdict"] == "control" and control["next_verdict"] is None and control["tries_to_next"] is None
    assert control["n"] == 30 and control["hit_rate"] == 1.0 and 0 < control["p_luck_alone"] < 0.001   # the warning light still reads
    # 30 right of 30 would be Proven for a contender on a board of one.
    assert reader.get(f"/agents/{reader.id}").json()["verdict"] == "proven"
    rows = {r["name"]: r for r in reader.get("/leaderboard").json()["agents"]}
    assert rows["momentum-control"]["verdict"] == "control" and rows["alpha"]["verdict"] == "proven"
    assert bots["momentum-control"].get("/me").json()["all_time"]["verdict"] == "control"


def test_reading_a_yardstick_s_call_costs_the_base_price(client, session_factory):
    from tests.conftest import set_price
    bots = board(client, session_factory, ["alpha", "momentum-control"])
    set_price(session_factory, "ETH", 100.0)
    bots["momentum-control"].post("/signals", {"pair": "ETH", "horizon": "15m", "direction": "long", "confidence": 0.8})
    bots["alpha"].post("/signals", {"pair": "ETH", "horizon": "15m", "direction": "long", "confidence": 0.8})
    listing = {r["agent_name"]: r for r in Agent(client, "reader").get("/room").json()["signals"]}
    assert (listing["momentum-control"]["agent_verdict"], listing["momentum-control"]["read_cost"]) == ("control", 1.0)
    assert (listing["alpha"]["agent_verdict"], listing["alpha"]["read_cost"]) == ("proven", 4.0)


def test_only_the_first_agent_under_a_yardstick_s_name_is_the_control(client, session_factory):
    """Names are not unique. A later agent that registers as "favourite-control" is an ordinary agent:
    it counts toward the board and is judged like anyone else."""
    real = Agent(client, "favourite-control")
    post_and_resolve(client, session_factory, real, 30, right=True)
    copycat = Agent(client, "favourite-control")
    post_and_resolve(client, session_factory, copycat, 30, right=True)
    spread(session_factory)
    db = session_factory()
    assert service.control_ids(db) == {real.id}
    assert service.board_size(db, service._since("90d")) == 1
    assert copycat.get(f"/agents/{copycat.id}").json()["verdict"] == "proven"
    assert copycat.get(f"/agents/{real.id}").json()["verdict"] == "control"
    db.close()


def test_delisted_still_outranks_control(client, session_factory):
    bots = board(client, session_factory, ["alpha", "momentum-control"])
    db = session_factory()
    db.get(AgentRow, bots["momentum-control"].id).status = "delisted"
    db.commit()
    db.close()
    assert bots["alpha"].get(f"/agents/{bots['momentum-control'].id}").json()["verdict"] == "delisted"


def test_the_status_page_and_the_scoreboard_say_control(client, session_factory):
    bots = board(client, session_factory, ["alpha", "momentum-control"])
    db = session_factory()
    text = status.report(db)
    assert "Control · right 100%" in text and "a yardstick" in text and "Proven · right 100%" in text
    assert feed.VERDICTS["control"] == "Control" and status.VERDICT_WORDS["control"] == "Control"
    db.close()


def test_the_list_of_yardsticks_is_fixed():
    assert config.CONTROL_AGENTS == ("momentum-control", "meanrev-control", "favourite-control", "reader-control")

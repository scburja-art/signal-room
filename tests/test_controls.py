"""The room's own yardstick bots are not competitors (approved by Steven 2026-11-11): they do not count
toward the size of the board, and they are shown as a control in place of a verdict. A yardstick is
known by its identity file in the room's own state directory, never by its name."""
import json

import pytest

from agents import reader_control
from signal_room import config, feed, report, service, status
from signal_room.db import Agent as AgentRow
from tests.api_helpers import Agent
from tests.conftest import set_price, spread
from tests.test_delisting import post_and_resolve

FILES = {"momentum-control": "momentum.json", "meanrev-control": "meanrev.json", "favourite-control": "favourite.json"}


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 10_000)


def board(client, factory, yardsticks, names, right=True):
    """Bots with 30 scored calls each. Those with a yardstick's name get the room's identity file."""
    bots = {name: Agent(client, name) for name in names}
    for name, bot in bots.items():
        if name in FILES:
            yardsticks(FILES[name], bot)
        post_and_resolve(client, factory, bot, 30, right=right)
    spread(factory)
    return bots


def test_yardsticks_do_not_count_toward_the_size_of_the_board(client, session_factory, yardsticks):
    # Two real bots and two yardsticks, each with 30 scored calls. The field is two, so the bar is 2 x 3.
    bots = board(client, session_factory, yardsticks, ["alpha", "beta", "momentum-control", "favourite-control"])
    db = session_factory()
    assert service.board_size(db, service._since("90d")) == 2
    rec = bots["alpha"].get(f"/agents/{bots['alpha'].id}").json()
    # By hand: the bar is raised 2 x 3 = 6 times, not 4 x 5 = 20.
    assert rec["competitors"] == 6 and rec["p_luck"] == pytest.approx(min(1.0, 6 * rec["p_luck_alone"]), rel=1e-5)
    db.close()


def test_a_real_bot_s_numbers_do_not_move_when_a_yardstick_joins(client, session_factory, yardsticks):
    bots = board(client, session_factory, yardsticks, ["alpha", "beta"])
    before = bots["alpha"].get(f"/agents/{bots['alpha'].id}").json()
    late = Agent(client, "favourite-control")
    yardsticks("favourite.json", late)
    post_and_resolve(client, session_factory, late, 30, right=True)
    spread(session_factory)
    after = bots["alpha"].get(f"/agents/{bots['alpha'].id}").json()
    for key in ("competitors", "p_luck", "p_worse", "verdict", "hit_rate_low", "hit_rate_high", "tries_to_next", "score"):
        assert after[key] == before[key], key


def test_a_yardstick_is_labelled_a_control_keeps_its_numbers_and_takes_no_rank(client, session_factory, yardsticks):
    bots = board(client, session_factory, yardsticks, ["alpha", "momentum-control"])
    reader = bots["alpha"]
    control = reader.get(f"/agents/{bots['momentum-control'].id}").json()
    assert control["verdict"] == "control" and control["next_verdict"] is None and control["tries_to_next"] is None
    assert control["n"] == 30 and control["hit_rate"] == 1.0 and 0 < control["p_luck_alone"] < 0.001   # the warning light still reads
    # 30 right of 30 would be Proven for a contender on a board of one.
    assert reader.get(f"/agents/{reader.id}").json()["verdict"] == "proven"
    rows = {r["name"]: r for r in reader.get("/leaderboard").json()["agents"]}
    assert (rows["momentum-control"]["verdict"], rows["momentum-control"]["rank"]) == ("control", None)
    assert (rows["alpha"]["verdict"], rows["alpha"]["rank"]) == ("proven", 1)
    assert bots["momentum-control"].get("/me").json()["all_time"]["verdict"] == "control"


def test_reading_a_yardstick_s_call_costs_the_base_price(client, session_factory, yardsticks):
    bots = board(client, session_factory, yardsticks, ["alpha", "momentum-control"])
    set_price(session_factory, "ETH", 100.0)
    bots["momentum-control"].post("/signals", {"pair": "ETH", "horizon": "15m", "direction": "long", "confidence": 0.8})
    bots["alpha"].post("/signals", {"pair": "ETH", "horizon": "15m", "direction": "long", "confidence": 0.8})
    listing = {r["agent_name"]: r for r in Agent(client, "reader").get("/room").json()["signals"]}
    assert (listing["momentum-control"]["agent_verdict"], listing["momentum-control"]["read_cost"]) == ("control", 1.0)
    assert (listing["alpha"]["agent_verdict"], listing["alpha"]["read_cost"]) == ("proven", 4.0)


def test_a_name_makes_nobody_a_yardstick(client, session_factory, yardsticks):
    """Review finding 2026-11-11: the first build went by name, so whoever registered "favourite-control"
    first was the control. Names are free text. Here a stranger takes the name before the room's bot does:
    the stranger is an ordinary agent, and the room's own bot, known by its identity file, is the control."""
    squatter = Agent(client, "favourite-control")
    post_and_resolve(client, session_factory, squatter, 30, right=True)
    real = Agent(client, "favourite-control")
    yardsticks("favourite.json", real)
    post_and_resolve(client, session_factory, real, 30, right=True)
    spread(session_factory)
    db = session_factory()
    assert service.control_ids(db) == {real.id}
    assert service.board_size(db, service._since("90d")) == 1          # the squatter counts; the real control does not
    assert squatter.get(f"/agents/{squatter.id}").json()["verdict"] == "proven"
    assert squatter.get(f"/agents/{real.id}").json()["verdict"] == "control"
    db.close()


def test_a_yardstick_registered_afresh_is_still_the_yardstick(client, session_factory, yardsticks):
    """Review finding 2026-11-11: a control whose wallet was retired or lost registers again under the same
    name; by name, the dead agent stayed the control and the live one raised everyone's bar."""
    old = Agent(client, "momentum-control")
    yardsticks("momentum.json", old)
    post_and_resolve(client, session_factory, old, 30, right=True)
    new = Agent(client, "momentum-control")
    yardsticks("momentum.json", new)                                    # the bot's identity file now names the new agent
    post_and_resolve(client, session_factory, new, 30, right=True)
    spread(session_factory)
    db = session_factory()
    assert service.control_ids(db) == {new.id}
    assert new.get(f"/agents/{new.id}").json()["verdict"] == "control"
    db.close()


def test_identity_files_that_are_missing_or_broken_name_nobody(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CONTROL_REFRESH_SECONDS", 0)
    monkeypatch.setattr(service, "_controls", None)
    assert service.control_ids() == frozenset()
    (tmp_path / "momentum.json").write_text("not json")
    (tmp_path / "meanrev.json").write_text("[1, 2]")
    (tmp_path / "favourite.json").write_text(json.dumps({"agent_id": 7}))
    (tmp_path / "reader.json").write_text(json.dumps({"agent_id": "abc"}))
    (tmp_path / "forecaster.json").write_text(json.dumps({"agent_id": "a-contender"}))     # not on the list of yardsticks
    assert service.control_ids() == {"abc"}
    monkeypatch.setattr(config, "CONTROL_IDS", ("x1", "x2"))            # the operator's own list replaces the files
    assert service.control_ids() == {"x1", "x2"}
    service._controls = None


def test_the_list_is_read_at_most_once_a_minute(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(service, "_controls", None)
    (tmp_path / "reader.json").write_text(json.dumps({"agent_id": "abc"}))
    assert service.control_ids() == {"abc"}
    (tmp_path / "reader.json").write_text(json.dumps({"agent_id": "changed"}))
    assert service.control_ids() == {"abc"}                             # within the minute: not read again
    service._controls = None


def test_delisted_still_outranks_control(client, session_factory, yardsticks):
    bots = board(client, session_factory, yardsticks, ["alpha", "momentum-control"])
    db = session_factory()
    db.get(AgentRow, bots["momentum-control"].id).status = "delisted"
    db.commit()
    db.close()
    assert bots["alpha"].get(f"/agents/{bots['momentum-control'].id}").json()["verdict"] == "delisted"


def test_the_status_page_and_the_scoreboard_say_control(client, session_factory, yardsticks):
    board(client, session_factory, yardsticks, ["alpha", "momentum-control"])
    db = session_factory()
    text = status.report(db)
    assert "Control · right 100%" in text and "a yardstick" in text and "Proven · right 100%" in text
    line = next(x for x in text.splitlines() if "a coin-flipper does this well" in x and "bar raised" not in x)
    assert line                                                         # the yardstick's own line claims no place on the board
    assert "  – momentum-control" in text and "  1. alpha" in text      # listed for comparison, without a rank
    assert feed.VERDICTS["control"] == "Control" and status.VERDICT_WORDS["control"] == "Control"
    db.close()


def test_a_losing_yardstick_s_light_points_the_other_way(client, session_factory, yardsticks):
    """Review finding 2026-11-11: with the label gone, a yardstick that was reliably wrong still read
    "does this well about even odds". Favourites losing to their price is the other half of the warning."""
    board(client, session_factory, yardsticks, ["momentum-control"], right=False)
    db = session_factory()
    text = status.report(db)
    assert "a coin-flipper does this badly" in text and "does this well" not in text
    db.close()


def test_the_reader_bot_prefers_real_authors_to_yardsticks():
    listing = [{"agent_id": "c", "signal_id": "1", "agent_verdict": "control", "agent_score": 0.9, "read_cost": 1.0},
               {"agent_id": "u", "signal_id": "2", "agent_verdict": "unproven", "agent_score": -0.5, "read_cost": 1.0}]
    assert reader_control.PREFERENCE[-1] == "control"
    assert reader_control.choose(listing, "me", set(), 10.0)["agent_id"] == "u"
    assert reader_control.choose(listing[:1], "me", set(), 10.0)["agent_id"] == "c"      # a yardstick is still better than nothing


def test_the_report_compares_like_with_like(client, session_factory, yardsticks):
    board(client, session_factory, yardsticks, ["momentum-control"])
    import datetime as dt
    from signal_room.db import utcnow
    text = report.report(session_factory(), now=utcnow() + dt.timedelta(hours=1))
    assert "verdict: Control (unchanged)" in text

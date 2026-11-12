"""`./start.sh status` must show the same numbers the API shows, in plain words."""
import pytest

from signal_room import config, status
from tests.conftest import spread
from tests.test_api import Agent as ApiAgent
from tests.test_delisting import post_and_resolve


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 10_000)


def test_status_matches_api(client, session_factory):
    good = ApiAgent(client, "good-bot")
    post_and_resolve(client, session_factory, good, 32, right=True, confidence=0.8)
    post_and_resolve(client, session_factory, good, 4, right=False, confidence=0.8)
    new = ApiAgent(client, "new-bot")
    post_and_resolve(client, session_factory, new, 3, right=True, confidence=0.6)

    spread(session_factory)   # the test posts in a burst; real calls are minutes apart

    text = status.report(session_factory())
    api = good.get("/leaderboard").json()["agents"]
    assert api[0]["name"] == "good-bot" and len(api) == 1
    # 32/36, each its own try. By hand: alone its odds are 7 / sum of (1+s)^32 (1-s)^4 over the seven stakes
    # = 0.000204, "proven". Two agents have been scored here, so the bar is 2 x 3 = 6: 0.00122, one step short.
    assert api[0]["p_luck_alone"] == pytest.approx(0.000204, abs=1e-6) and api[0]["p_luck"] == pytest.approx(0.001223, abs=1e-6)
    assert "1. good-bot  Probably not luck · right 89%" in text
    assert f"skill {api[0]['skill_points']:+d}" in text
    assert "scored 36, flat 0, open 0, voided 0" in text
    assert "new-bot  (active)\n    Unproven · right 100%" in text
    assert "needs 27 more scored signals to be ranked" in text
    assert "WARNING" not in text


def test_status_warns_when_sampler_is_down(session_factory):
    text = status.report(session_factory())
    assert "nobody ranked yet" in text
    assert "price sampler is not running" in text


def test_a_priced_record_is_read_against_what_it_cost(client, session_factory):
    from tests.test_priced import T0, rows_for
    from signal_room.db import Agent as AgentRow, Signal
    a = ApiAgent(client, "kakalshi-crypto-first-minute-priced")
    rows_for(session_factory, a.id, 75, 15, bar=0.70, stated=0.80)      # wins 83% at a 70c cost
    s = session_factory()
    s.add(Signal(agent_id=a.id, pair="BTC", horizon="15m", kind="priced", venue="kalshi", market="KXBTC15M-X-00", body_cipher="-",
                 hash="f" * 64, hash_version=4, submitted_at=T0, resolve_at=T0, resolved_at=T0, fill="unfilled", result="unfilled", credit=0.0))
    s.commit()
    text = "\n".join(status.agent_lines(s, s.get(AgentRow, a.id), T0))
    assert "right 83% against a bar of 70% (edge +13, range " in text
    assert "a bot with no edge over its prices does this well about 1 in " in text and "coin-flipper" not in text
    assert "scored 90, flat 0, open 0, voided 0, not filled 1, credits" in text
    assert "priced calls: 90 bought at the price the room read; average profit +13.3¢ a contract after fees" in text
    assert "traded 1:1" not in text                                     # nothing here was a 1:1 trade
    s.close()

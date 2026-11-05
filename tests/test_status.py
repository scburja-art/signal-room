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
    assert "1. good-bot  Proven · right 89%" in text          # 32/36, each its own try
    assert f"skill {api[0]['skill_points']:+d}" in text
    assert "scored 36, flat 0, open 0, voided 0" in text
    assert "new-bot  (active)\n    Unproven · right 100%" in text
    assert "needs 27 more scored signals to be ranked" in text
    assert "WARNING" not in text


def test_status_warns_when_sampler_is_down(session_factory):
    text = status.report(session_factory())
    assert "nobody ranked yet" in text
    assert "price sampler is not running" in text

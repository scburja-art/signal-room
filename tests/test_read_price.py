"""Reading an open call costs more when its author has earned a stronger verdict on that window."""
import datetime as dt

import pytest

from signal_room import config, service
from signal_room.db import Signal
from tests.api_helpers import Agent
from tests.conftest import set_price, spread
from tests.test_delisting import post_and_resolve


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 10_000)


def test_read_cost_follows_the_authors_verdict(client, session_factory):
    newbie, expert, reader = Agent(client, "newbie"), Agent(client, "expert"), Agent(client, "reader")
    post_and_resolve(client, session_factory, expert, 40, right=True, confidence=0.8)   # 40 of 40, each its own try
    spread(session_factory, expert.id)

    set_price(session_factory, "ETH", 2000.0)
    set_price(session_factory, "BTC", 100.0)
    cheap = newbie.post("/signals", {"pair": "ETH", "horizon": "15m", "direction": "long", "confidence": 0.7}).json()["signal_id"]
    dear = expert.post("/signals", {"pair": "BTC", "horizon": "15m", "direction": "long", "confidence": 0.8}).json()["signal_id"]

    listing = {x["signal_id"]: x for x in reader.get("/room").json()["signals"]}
    assert listing[cheap]["agent_verdict"] == "unproven" and listing[cheap]["read_cost"] == 1.0
    assert listing[dear]["agent_verdict"] == "proven" and listing[dear]["read_cost"] == 4.0

    assert reader.get(f"/room/{cheap}").status_code == 200
    assert reader.get("/me").json()["credits"] == pytest.approx(9.0)
    assert reader.get(f"/room/{dear}").status_code == 200
    assert reader.get("/me").json()["credits"] == pytest.approx(5.0)
    assert reader.get(f"/room/{dear}").status_code == 200          # second read is free
    assert reader.get("/me").json()["credits"] == pytest.approx(5.0)
    assert service.read_cost("something-else") == config.READ_COST

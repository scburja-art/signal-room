"""Run a synthetic agent through delist → reinstate → retire using the real resolve path and real-shaped signals."""
import datetime as dt

import pytest

from signal_room import config, service
from signal_room.db import Agent, AuditEvent, Signal
from tests.conftest import set_price
from tests.test_api import Agent as ApiAgent


def post_and_resolve(client, factory, agent, n, right, confidence=0.8, pair="BTC"):
    """Post n signals and resolve each; `right` decides whether price moves with the call."""
    from signal_room.db import ReferencePrice
    for i in range(n):
        s = factory()
        s.query(ReferencePrice).filter_by(pair=pair).delete()  # fresh tape each round
        s.commit()
        s.close()
        set_price(factory, pair, 100.0)
        r = agent.post("/signals", {"pair": pair, "horizon": "15m", "direction": "long", "confidence": confidence})
        assert r.status_code == 201, r.text
        s = factory()
        sig = s.query(Signal).filter_by(id=r.json()["signal_id"]).first()
        assert sig.price_at_submit == 100.0
        set_price(factory, pair, 101.0 if right else 99.0, when=sig.resolve_at)
        service.resolve_due(s, now=sig.resolve_at + dt.timedelta(seconds=1))
        s.refresh(sig)
        assert sig.result == ("right" if right else "wrong"), sig.result
        s.close()


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    """These tests post hundreds of signals in seconds; the per-minute limit is tested elsewhere."""
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 10_000)


def post_wrong_until(client, factory, agent, status, pair, cap=100):
    """Post confidently-wrong signals one at a time until the agent reaches `status`. Returns how many it took."""
    for i in range(1, cap + 1):
        post_and_resolve(client, factory, agent, 1, right=False, pair=pair)
        if status_of(factory, agent.id) == status:
            return i
    raise AssertionError(f"never reached {status} after {cap} wrong signals")


def status_of(factory, agent_id):
    s = factory()
    st = s.get(Agent, agent_id).status
    s.close()
    return st


def test_coin_flip_agent_is_never_delisted(client, session_factory):
    a = ApiAgent(client, "flip")
    post_and_resolve(client, session_factory, a, 95, right=True, confidence=0.5)
    assert status_of(session_factory, a.id) == "active"


def test_delist_reinstate_retire(client, session_factory):
    bad = ApiAgent(client, "bad")
    reader = ApiAgent(client, "reader")

    # 89 confidently-wrong signals: still active (window not full)
    post_and_resolve(client, session_factory, bad, 89, right=False)
    assert status_of(session_factory, bad.id) == "active"
    # the 90th crosses the line
    post_and_resolve(client, session_factory, bad, 1, right=False)
    assert status_of(session_factory, bad.id) == "delisted"

    # delisted: can post, cannot read the room; its open signals are hidden from others
    set_price(session_factory, "ETH", 10.0)
    r = bad.post("/signals", {"pair": "ETH", "horizon": "1h", "direction": "long", "confidence": 0.7})
    assert r.status_code == 201
    assert bad.get("/room").status_code == 403
    assert all(x["agent_id"] != bad.id for x in reader.get("/room").json()["signals"])
    assert reader.get("/leaderboard").json()["agents"] == []
    me = bad.get("/me").json()
    assert me["status"] == "delisted" and me["delist_count_12mo"] == 1

    # 29 good fresh signals: not yet; 30th with BSS > 0 reinstates
    post_and_resolve(client, session_factory, bad, 29, right=True, pair="SOL")
    assert status_of(session_factory, bad.id) == "delisted"
    post_and_resolve(client, session_factory, bad, 1, right=True, pair="SOL")
    assert status_of(session_factory, bad.id) == "active"
    assert bad.get("/room").status_code == 200

    # Second delisting: the clean window holds the 30 reinstatement wins, so it takes 60 more wrongs
    # (30 right + 60 wrong = 90, BSS well below -0.10), not 1 and not 90.
    took = post_wrong_until(client, session_factory, bad, "delisted", pair="XRP")
    assert took == config.DELIST_WINDOW - config.REINSTATE_MIN_RESOLVED, took
    post_and_resolve(client, session_factory, bad, 30, right=True, pair="XRP")
    assert status_of(session_factory, bad.id) == "active"
    # Third delisting within the year → retired, and retired cannot post
    post_wrong_until(client, session_factory, bad, "retired", pair="XRP")
    set_price(session_factory, "BTC", 100.0)
    assert bad.post("/signals", {"pair": "BTC", "horizon": "4h", "direction": "long", "confidence": 0.7}).status_code == 403

    s = session_factory()
    events = [e.event for e in s.query(AuditEvent).filter_by(agent_id=bad.id).order_by(AuditEvent.id).all()]
    s.close()
    assert events.count("delisted") == 3 and events.count("reinstated") == 2 and events[-1] == "retired"


def test_migration_adds_delisted_at(tmp_path):
    """An old database without the column gets it on init."""
    from sqlalchemy import create_engine, text
    from signal_room.db import init_db
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE agents (id VARCHAR(32) PRIMARY KEY, wallet VARCHAR(64), api_key_hash VARCHAR(64), name VARCHAR(64), status VARCHAR(16), delist_count INTEGER, created_at DATETIME)"))
    init_db(eng)
    with eng.begin() as c:
        cols = [r[1] for r in c.execute(text("PRAGMA table_info(agents)"))]
    assert "delisted_at" in cols

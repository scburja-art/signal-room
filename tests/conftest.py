import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SIGNAL_ROOM_AGENT_STATE"] = __import__("tempfile").mkdtemp(prefix="signal-room-no-controls-")   # no yardsticks unless a test makes some
os.environ["TZ"] = "America/New_York"   # the feed prints local time; tests read the same on any machine
__import__("time").tzset()
os.environ["SIGNAL_ROOM_MASTER_KEY"] = "zJ9S5n8rQw1pYv0K3mC7xT2bL6aH4dF8gE1uN5sR9oI="

import datetime as dt  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from signal_room import db as dbmod  # noqa: E402
from signal_room.db import Base, ReferencePrice, utcnow  # noqa: E402


@pytest.fixture()
def session_factory(monkeypatch, request, tmp_path):
    if request.node.get_closest_marker("filedb"):
        # Real file with per-thread connections, for tests that need true concurrency.
        engine = create_engine(f"sqlite:///{tmp_path}/t.db", connect_args={"check_same_thread": False, "timeout": 30})
    else:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SessionLocal", factory)
    import signal_room.scheduler as sched
    monkeypatch.setattr(sched, "SessionLocal", factory)

    def _get_session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    from signal_room.api import app
    from signal_room.db import get_session
    app.dependency_overrides[get_session] = _get_session
    yield factory
    app.dependency_overrides.clear()


@pytest.fixture()
def client(session_factory, monkeypatch):
    from signal_room import api, config
    monkeypatch.setattr(config, "REGISTRATIONS_PER_HOUR", 10_000)
    monkeypatch.setattr(config, "CHALLENGES_PER_HOUR_PER_IP", 10_000)
    api._reg_times.clear()
    api._challenge_times.clear()
    return TestClient(api.app)


@pytest.fixture()
def venue(monkeypatch):
    """A stand-in for Kalshi's public data, wired into the room's reader. No test touches the network."""
    from signal_room import kalshi
    from tests.kalshi_fake import FakeKalshi
    fake = FakeKalshi()
    monkeypatch.setattr(kalshi, "_http", fake.client())
    from agents import verify
    monkeypatch.setattr(verify, "_kalshi_http", fake.client())   # the outside checker has its own reader
    kalshi._series_ok.clear()
    yield fake
    kalshi._series_ok.clear()


def set_price(factory, pair: str, price: float, when: dt.datetime | None = None):
    s = factory()
    s.add(ReferencePrice(pair=pair, price=price, feeds_used=3, sampled_at=when or utcnow()))
    s.commit()
    s.close()


def start(factory, signal_id: str, price: float, when: dt.datetime | None = None):
    """Give a freshly posted call its start price: a sample taken after the post (rule set 3), then
    the fill. Returns the signal with its final resolve_at."""
    from signal_room import service
    from signal_room.db import Signal
    s = factory()
    sig = s.query(Signal).filter_by(id=signal_id).first()
    at = when or sig.submitted_at + dt.timedelta(seconds=1)
    s.add(ReferencePrice(pair=sig.pair, price=price, feeds_used=3, sampled_at=at))
    s.commit()
    service.fill_start_prices(s)
    s.refresh(sig)
    s.close()
    return sig


def spread(factory, agent_id: str | None = None, minutes: int = 15, end: dt.datetime | None = None):
    """Test helpers post in a burst; real calls are minutes apart. Space the calls back in time so
    each window closes before the next opens and every call is its own try. The last call ends at
    `end` (default: now)."""
    from signal_room import config
    from signal_room.db import Signal
    s = factory()
    q = s.query(Signal)
    if agent_id:
        q = q.filter_by(agent_id=agent_id)
    sigs = q.order_by(Signal.submitted_at).all()
    end = end or utcnow()
    for i, sig in enumerate(sigs):
        sig.submitted_at = end - dt.timedelta(minutes=minutes * (len(sigs) - i)) - dt.timedelta(seconds=1)
        sig.start_at = sig.submitted_at + dt.timedelta(seconds=1)
        sig.resolve_at = sig.start_at + dt.timedelta(seconds=config.HORIZONS[sig.horizon])
        if sig.resolved_at is not None:
            sig.resolved_at = sig.resolve_at
    s.commit()
    s.close()


@pytest.fixture()
def yardsticks(monkeypatch, tmp_path):
    """Make agents the room's own yardsticks, the way the seed bots do: an identity file in the room's
    state directory. yardsticks("momentum.json", agent) writes one; the room re-reads at once."""
    from signal_room import config, service
    monkeypatch.setattr(config, "AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CONTROL_REFRESH_SECONDS", 0)
    monkeypatch.setattr(service, "_controls", None)
    monkeypatch.setattr(service, "_closed", None)

    def make(filename, agent):
        import json
        (tmp_path / filename).write_text(json.dumps({"chain": "evm", "private_key": "not-read", "api_key": "not-read", "agent_id": agent.id}))
    yield make
    service._controls = None
    service._closed = None

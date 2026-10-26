import os

os.environ["DATABASE_URL"] = "sqlite://"
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
def session_factory(monkeypatch):
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
def client(session_factory):
    from signal_room.api import app
    return TestClient(app)


def set_price(factory, pair: str, price: float, when: dt.datetime | None = None):
    s = factory()
    s.add(ReferencePrice(pair=pair, price=price, feeds_used=3, sampled_at=when or utcnow()))
    s.commit()
    s.close()

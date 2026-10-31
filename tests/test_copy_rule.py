"""After reading another agent's open call on a coin, you cannot post on that coin until it closes."""
import datetime as dt

from signal_room import service
from signal_room.db import Signal
from tests.api_helpers import Agent
from tests.conftest import set_price


def test_reader_cannot_repost_a_call_it_read(client, session_factory):
    author, copier = Agent(client, "author"), Agent(client, "copier")
    for pair, price in (("BTC", 100.0), ("ETH", 2000.0)):
        set_price(session_factory, pair, price)
    sid = author.post("/signals", {"pair": "BTC", "horizon": "1h", "direction": "long", "confidence": 0.8}).json()["signal_id"]

    # Before reading, the copier may post BTC freely.
    assert copier.post("/signals", {"pair": "BTC", "horizon": "4h", "direction": "short", "confidence": 0.6}).status_code == 201
    assert copier.get(f"/room/{sid}").status_code == 200

    # After reading: no BTC on any window, other coins fine.
    for horizon in ("15m", "1h", "24h"):
        r = copier.post("/signals", {"pair": "BTC", "horizon": horizon, "direction": "long", "confidence": 0.8})
        assert r.status_code == 409 and "you read an open BTC call" in r.text
    assert copier.post("/signals", {"pair": "ETH", "horizon": "1h", "direction": "long", "confidence": 0.6}).status_code == 201

    # The author is not blocked by its own call being read.
    assert author.post("/signals", {"pair": "BTC", "horizon": "15m", "direction": "long", "confidence": 0.6}).status_code == 201

    # Once the read call closes, the lock lifts.
    s = session_factory()
    sig = s.get(Signal, sid)
    set_price(session_factory, "BTC", 101.0, when=sig.resolve_at)
    service.resolve_due(s, now=sig.resolve_at + dt.timedelta(seconds=5))
    s.close()
    set_price(session_factory, "BTC", 101.0)
    assert copier.post("/signals", {"pair": "BTC", "horizon": "1h", "direction": "long", "confidence": 0.8}).status_code == 201

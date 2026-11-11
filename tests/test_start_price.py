"""Rule set 3 (approved 2026-11-04): a call's scored window starts at the first reference sample taken
AFTER the post, so a bot watching live prices cannot cash in a move the room has not sampled yet."""
import datetime as dt

import pytest

from signal_room import config, scoring, service
from signal_room.db import ReferencePrice, Signal
from signal_room.sealing import leaf_hash, signal_hash, unseal
from tests.api_helpers import Agent
from tests.conftest import set_price


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 10_000)


def test_start_is_the_first_sample_after_the_post_never_before(client, session_factory):
    a = Agent(client, "watcher")
    # The sampler's last sample said 100. The live market has since moved to 103 and the bot knows it.
    set_price(session_factory, "BTC", 100.0, when=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(seconds=50))
    r = a.post("/signals", {"pair": "BTC", "horizon": "15m", "direction": "long", "confidence": 0.9})
    assert r.status_code == 201
    assert r.json()["price_at_submit"] is None and "first reference sample after this post" in r.json()["start"]
    s = session_factory()
    sig = s.query(Signal).first()
    assert sig.price_at_submit is None and sig.start_at is None and sig.hash_version == 3
    assert sig.resolve_at == sig.submitted_at + dt.timedelta(minutes=15)       # provisional
    # The next sample catches the move the bot already saw: the window starts at 103, not 100.
    at = sig.submitted_at + dt.timedelta(seconds=20)
    set_price(session_factory, "BTC", 103.0, when=at)
    assert service.fill_start_prices(s) == 1
    sig = s.query(Signal).first()
    assert sig.price_at_submit == 103.0 and sig.start_at == at
    assert sig.resolve_at == at + dt.timedelta(minutes=15)                    # start + horizon
    # Resolving from 103 to 103: flat, so the "free" move earned nothing.
    set_price(session_factory, "BTC", 103.0, when=sig.resolve_at)
    assert service.resolve_due(s, now=sig.resolve_at + dt.timedelta(seconds=1)) == 1
    sig = s.query(Signal).first()
    assert sig.result == "flat" and sig.scoring_version == scoring.SCORING_VERSION == 4


def test_hash_commits_to_the_call_and_the_root_to_its_start(client, session_factory):
    a = Agent(client, "h")
    set_price(session_factory, "ETH", 10.0)
    sid = a.post("/signals", {"pair": "ETH", "horizon": "1h", "direction": "short", "confidence": 0.7}).json()["signal_id"]
    s = session_factory()
    sig = s.get(Signal, sid)
    body = unseal(sig.agent_id, sig.body_cipher)
    # No price in the receipt (it is not known yet); the receipt still commits to the prediction.
    assert sig.hash == signal_hash(sig.agent_id, "ETH", "1h", "short", 0.7, sig.submitted_at.isoformat(), None, body["salt"])
    assert service.leaf(sig) == sig.hash                                        # no start yet: the receipt stands in
    assert not service.day_ready_to_anchor(s, sig.submitted_at.date())           # so the day cannot be anchored yet
    set_price(session_factory, "ETH", 10.5, when=sig.submitted_at + dt.timedelta(seconds=5))
    service.fill_start_prices(s)
    sig = s.get(Signal, sid)
    assert service.leaf(sig) == leaf_hash(sig.hash, sig.start_at.isoformat(), 10.5)
    assert service.day_ready_to_anchor(s, sig.submitted_at.date())
    log = a.get(f"/log/{sig.submitted_at.date().isoformat()}").json()
    assert log["signals"][0]["leaf"] == service.leaf(sig) and log["signals"][0]["price_at_submit"] == 10.5


def test_no_sample_for_three_days_voids_the_call(client, session_factory, monkeypatch):
    from signal_room import prices
    monkeypatch.setattr(prices, "CANDLE_FEEDS", {})
    a = Agent(client, "v")
    set_price(session_factory, "SOL", 50.0)
    sid = a.post("/signals", {"pair": "SOL", "horizon": "15m", "direction": "long", "confidence": 0.6}).json()["signal_id"]
    s = session_factory()
    sig = s.get(Signal, sid)
    assert service.resolve_due(s, now=sig.resolve_at + dt.timedelta(hours=1)) == 0     # still inside the backfill window: waits
    assert service.resolve_due(s, now=sig.submitted_at + dt.timedelta(hours=73)) == 1
    assert s.get(Signal, sid).result == "void"


def test_posting_needs_a_live_sampler(client, session_factory):
    a = Agent(client, "x")
    set_price(session_factory, "XRP", 1.0, when=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=10))
    assert a.post("/signals", {"pair": "XRP", "horizon": "15m", "direction": "long", "confidence": 0.6}).status_code == 503


def test_calls_posted_under_the_old_rule_keep_their_start(client, session_factory):
    """A call that already had its start price (rule set 2) resolves under rule set 2 and is scored as before."""
    a = Agent(client, "old")
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    s = session_factory()
    s.add(Signal(id="old1", agent_id=a.id, pair="BTC", horizon="15m", hash="ab" * 32, hash_version=2,
                 body_cipher=__import__("signal_room.sealing", fromlist=["seal"]).seal(a.id, {"direction": "long", "confidence": 0.6, "salt": "00" * 16}),
                 submitted_at=now - dt.timedelta(minutes=16), resolve_at=now - dt.timedelta(minutes=1), price_at_submit=100.0))
    s.commit()
    set_price(session_factory, "BTC", 101.0, when=now - dt.timedelta(minutes=1))
    assert service.resolve_due(s, now=now) == 1
    sig = s.get(Signal, "old1")
    assert sig.result == "right" and sig.price_at_submit == 100.0 and sig.scoring_version == 2

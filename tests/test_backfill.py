"""A signal that comes due while the sampler was asleep is scored from exchange candles, not voided."""
import datetime as dt

import pytest

from signal_room import config, prices, service
from signal_room.db import AuditEvent, ReferencePrice, Signal
from tests.conftest import set_price
from tests.test_api import Agent


def fake_candles(series: dict[dt.datetime, float]):
    def fn(client, pair, start, end):
        return {t: v for t, v in series.items() if start <= t <= end + dt.timedelta(seconds=60)}
    return fn


def test_backfill_fills_only_missing_minutes(session_factory, monkeypatch):
    s = session_factory()
    base = dt.datetime(2026, 10, 30, 3, 0)
    set_price(session_factory, "BTC", 100.0, when=base + dt.timedelta(minutes=2))  # one minute already live
    series = {base + dt.timedelta(minutes=i): 100.0 + i for i in range(6)}
    monkeypatch.setattr(prices, "CANDLE_FEEDS", {"a": fake_candles(series), "b": fake_candles(series), "c": fake_candles({})})
    n = prices.backfill(s, "BTC", base, base + dt.timedelta(minutes=5), client=object())
    assert n == 5
    rows = s.query(ReferencePrice).filter_by(pair="BTC").order_by(ReferencePrice.sampled_at).all()
    assert [r.source for r in rows] == ["backfill", "backfill", "live", "backfill", "backfill", "backfill"]
    assert rows[3].price == 103.0 and rows[3].feeds_used == 2
    assert s.query(AuditEvent).filter_by(event="gap_backfilled").count() == 1
    # one feed alone is not enough to write a row
    monkeypatch.setattr(prices, "CANDLE_FEEDS", {"a": fake_candles({base + dt.timedelta(minutes=9): 1.0})})
    assert prices.backfill(s, "BTC", base + dt.timedelta(minutes=9), base + dt.timedelta(minutes=9), client=object()) == 0
    s.close()


def test_signal_due_during_sleep_is_scored_not_voided(client, session_factory, monkeypatch):
    a = Agent(client, "sleeper")
    set_price(session_factory, "ETH", 100.0)
    sid = a.post("/signals", {"pair": "ETH", "horizon": "15m", "direction": "long", "confidence": 0.8}).json()["signal_id"]
    s = session_factory()
    sig = s.query(Signal).filter_by(id=sid).first()
    # No live prices around the horizon (laptop was asleep). Exchanges have the candles.
    series = {sig.resolve_at.replace(second=0, microsecond=0) + dt.timedelta(minutes=i): 102.0 for i in range(-2, 3)}
    calls = []
    def fn(client_, pair, start, end):
        calls.append(pair)
        return {t: v for t, v in series.items() if start <= t <= end + dt.timedelta(seconds=60)}
    monkeypatch.setattr(prices, "CANDLE_FEEDS", {"a": fn, "b": fn})
    monkeypatch.setattr(prices.httpx, "Client", lambda *a, **k: type("C", (), {"close": lambda self: None})())
    woke = sig.resolve_at + dt.timedelta(hours=6)
    assert service.resolve_due(s, now=woke) == 1
    s.refresh(sig)
    assert sig.result == "right" and sig.price_at_resolve == 102.0 and calls
    # and if the exchanges are unreachable on wake, it waits rather than voiding
    set_price(session_factory, "SOL", 50.0)
    sid2 = a.post("/signals", {"pair": "SOL", "horizon": "15m", "direction": "long", "confidence": 0.8}).json()["signal_id"]
    sig2 = s.query(Signal).filter_by(id=sid2).first()
    monkeypatch.setattr(prices, "CANDLE_FEEDS", {"a": lambda *a: (_ for _ in ()).throw(RuntimeError("offline"))})
    assert service.resolve_due(s, now=sig2.resolve_at + dt.timedelta(hours=1)) == 0
    s.refresh(sig2)
    assert sig2.result is None
    # past the backfill window it finally voids
    assert service.resolve_due(s, now=sig2.resolve_at + dt.timedelta(hours=config.BACKFILL_WINDOW_HOURS + 1)) == 1
    s.refresh(sig2)
    assert sig2.result == "void"
    s.close()


def test_migration_adds_source_column(tmp_path):
    from sqlalchemy import create_engine, text
    from signal_room.db import init_db
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE agents (id VARCHAR(32) PRIMARY KEY, wallet VARCHAR(64), api_key_hash VARCHAR(64), name VARCHAR(64), status VARCHAR(16), delist_count INTEGER, created_at DATETIME)"))
        c.execute(text("CREATE TABLE signals (id VARCHAR(32) PRIMARY KEY, agent_id VARCHAR(32), pair VARCHAR(8), horizon VARCHAR(4), body_cipher TEXT, hash VARCHAR(64), submitted_at DATETIME, resolve_at DATETIME, price_at_submit FLOAT, resolved_at DATETIME, result VARCHAR(5), brier FLOAT, credit FLOAT)"))
        c.execute(text("CREATE TABLE reference_prices (id INTEGER PRIMARY KEY, pair VARCHAR(8), price FLOAT, feeds_used INTEGER, sampled_at DATETIME)"))
        c.execute(text("INSERT INTO reference_prices (pair, price, feeds_used, sampled_at) VALUES ('BTC', 1, 3, '2026-10-29')"))
    init_db(eng)
    with eng.begin() as c:
        assert c.execute(text("SELECT source FROM reference_prices")).fetchone()[0] == "live"

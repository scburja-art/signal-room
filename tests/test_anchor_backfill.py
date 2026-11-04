"""Every closed day gets anchored once, oldest first, one per tick; today never does."""
import datetime as dt

from signal_room import anchor, scheduler
from signal_room.db import DailyLog, Signal, utcnow


def add_signal(factory, when):
    s = factory()
    s.add(Signal(agent_id="a", pair="BTC", horizon="15m", body_cipher="x", hash="ab" * 32,
                 submitted_at=when, resolve_at=when + dt.timedelta(minutes=15), price_at_submit=1.0,
                 resolved_at=when + dt.timedelta(minutes=15), result="void"))
    s.commit()
    s.close()


def test_backfill_oldest_first_one_per_tick(session_factory, monkeypatch):
    today = utcnow().date()
    start = dt.datetime.combine(today - dt.timedelta(days=4), dt.time(12))
    add_signal(session_factory, start)                              # day -4 has a call
    add_signal(session_factory, start + dt.timedelta(days=2))       # day -2 has a call; -3 and -1 are empty
    add_signal(session_factory, dt.datetime.combine(today, dt.time(0, 5)))   # today

    sent = []

    def fake_anchor(db, day_log):
        sent.append(day_log.day)
        day_log.anchored_tx = f"0x{len(sent):064x}"
        db.commit()
        return day_log.anchored_tx

    monkeypatch.setattr(anchor, "enabled", lambda: True)
    monkeypatch.setattr(anchor, "anchor_day", fake_anchor)
    monkeypatch.setattr(scheduler.prices, "sample_all", lambda db: [])
    for _ in range(6):
        scheduler.tick()

    expected = [(today - dt.timedelta(days=k)).isoformat() for k in (4, 3, 2, 1)]
    assert sent == expected                                          # oldest first, empty days included, no repeats
    s = session_factory()
    assert s.get(DailyLog, today.isoformat()).anchored_tx is None   # today is still open
    assert s.get(DailyLog, expected[0]).signal_count == 1 and s.get(DailyLog, expected[1]).signal_count == 0
    s.close()
    assert scheduler.oldest_unanchored(session_factory(), today) is None


def test_a_failed_anchor_is_retried_next_tick(session_factory, monkeypatch):
    today = utcnow().date()
    add_signal(session_factory, dt.datetime.combine(today - dt.timedelta(days=1), dt.time(9)))
    calls = []

    def flaky(db, day_log):
        calls.append(day_log.day)
        if len(calls) == 1:
            raise RuntimeError("rpc down")
        day_log.anchored_tx = "0x" + "1" * 64
        db.commit()
        return day_log.anchored_tx

    monkeypatch.setattr(anchor, "enabled", lambda: True)
    monkeypatch.setattr(anchor, "anchor_day", flaky)
    monkeypatch.setattr(scheduler.prices, "sample_all", lambda db: [])
    scheduler.tick()
    scheduler.tick()
    scheduler.tick()
    assert calls == [(today - dt.timedelta(days=1)).isoformat()] * 2

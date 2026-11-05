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


def test_one_transaction_per_day_even_when_mining_is_slow(session_factory, monkeypatch):
    """A sent-but-unmined anchor is remembered and polled; a second one is never sent."""
    from signal_room import anchor as an
    sent, settled = [], []

    class Eth:
        account = type("A", (), {"from_key": staticmethod(lambda k: type("K", (), {
            "address": "0xabc", "sign_transaction": lambda self, tx: type("S", (), {"raw_transaction": b"\x01"})()})())})()
        gas_price = 1
        def get_block(self, _): return {"baseFeePerGas": 100}
        def get_transaction_count(self, a, b): return len(sent)
        def estimate_gas(self, tx): return 21000
        def send_raw_transaction(self, raw):
            sent.append(raw)
            return "0x" + f"{len(sent):064x}"
        def wait_for_transaction_receipt(self, h, timeout):
            if len(settled) < 2:          # first two polls: not mined yet
                settled.append(h)
                raise TimeoutError("pending")
            return {"status": 1}

    class W3:
        eth = Eth()
        def to_wei(self, v, u): return 1
    monkeypatch.setattr(an, "PRIVATE_KEY", "k")
    monkeypatch.setattr(an, "_w3", lambda: W3())
    s = session_factory()
    s.add(DailyLog(day="2026-11-01", merkle_root="ab" * 32, signal_count=0))
    s.commit()
    day_log = s.get(DailyLog, "2026-11-01")
    assert an.anchor_day(s, day_log) is None and day_log.pending_tx and day_log.anchored_tx is None   # sent, pending
    assert an.anchor_day(s, day_log) is None and len(sent) == 1                                        # polled, not resent
    assert an.anchor_day(s, day_log) == day_log.pending_tx or day_log.anchored_tx                     # mined now
    assert len(sent) == 1 and day_log.anchored_tx and day_log.pending_tx is None


def test_a_day_with_a_call_missing_its_start_waits(session_factory, monkeypatch):
    from signal_room import service
    today = utcnow().date()
    when = dt.datetime.combine(today - dt.timedelta(days=1), dt.time(23, 59))
    s = session_factory()
    s.add(Signal(agent_id="a", pair="BTC", horizon="24h", body_cipher="x", hash="cd" * 32, hash_version=3,
                 submitted_at=when, resolve_at=when + dt.timedelta(days=1), price_at_submit=None))
    s.commit()
    sent = []
    monkeypatch.setattr(anchor, "enabled", lambda: True)
    monkeypatch.setattr(anchor, "anchor_day", lambda db, dl: sent.append(dl.day))
    assert scheduler.anchor_step(s) is None and sent == []           # not until the call has its start
    sig = s.query(Signal).first()
    sig.price_at_submit, sig.start_at = 1.0, when + dt.timedelta(seconds=30)
    s.commit()
    scheduler.anchor_step(s)
    assert sent == [when.date().isoformat()]

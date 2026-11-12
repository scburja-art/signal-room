"""`./start.sh report` must count the window exactly and say when a verdict moved."""
import datetime as dt

import pytest

from signal_room import config, report
from signal_room.db import Signal
from tests.test_api import Agent as ApiAgent
from tests.test_delisting import post_and_resolve
from tests.conftest import spread


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 10_000)


def test_bar_marks_coin_flip():
    assert report.bar(0.5) == "██████████│··········"
    assert report.bar(0.75) == "██████████│█████·····"
    assert report.bar(0.0) == "··········│··········"


def test_report_counts_window_and_verdict_change(client, session_factory):
    good = ApiAgent(client, "forecaster-v1")
    post_and_resolve(client, session_factory, good, 30, right=True, confidence=0.8)
    post_and_resolve(client, session_factory, good, 3, right=False, confidence=0.8)

    # Spread the calls 15 minutes apart so each is its own try. The last 10 close inside the report window.
    now = dt.datetime(2026, 11, 4, 12, 0)
    spread(session_factory, end=now - dt.timedelta(hours=30))
    db = session_factory()
    sigs = db.query(Signal).order_by(Signal.submitted_at).all()
    for i, sig in enumerate(sigs[23:]):
        sig.submitted_at = now - dt.timedelta(hours=20) + dt.timedelta(minutes=15 * i)
        sig.start_at = sig.submitted_at + dt.timedelta(seconds=1)
        sig.resolve_at = sig.resolved_at = sig.start_at + dt.timedelta(minutes=15)
    db.commit()

    text = report.report(session_factory(), now=now)
    assert "10 new calls, 10 closed: 7 right, 3 wrong (70% right" in text
    assert "Forecaster v1\n  ██████████│████······  right 70% (7 of 10)" in text
    # Before the window: 23 scored, under the 30 needed, so Unproven. After: 33 scored.
    assert "verdict moved: Unproven →" in text

    day = report.report(session_factory(), day=dt.date(2026, 11, 2))
    assert "Signal Room, 2026-11-02 (UTC day)" in day


def test_free_reads_and_pre_launch_days_not_counted(client, session_factory):
    from signal_room.db import DailyLog, utcnow
    author = ApiAgent(client, "momentum-control")
    post_and_resolve(client, session_factory, author, 1, right=True)
    sig_id = session_factory().query(Signal).first().id
    db = session_factory()
    for sig in db.query(Signal):
        sig.resolved_at = utcnow() - dt.timedelta(minutes=5)   # closed: reading it is free
    first_day = db.query(Signal).first().submitted_at.date()
    db.add(DailyLog(day=(first_day - dt.timedelta(days=1)).isoformat(), merkle_root="0" * 64, signal_count=0))
    db.add(DailyLog(day=(first_day - dt.timedelta(days=2)).isoformat(), merkle_root="0" * 64, signal_count=0,
                    anchored_tx="0xabc"))
    db.commit()
    verifier = ApiAgent(client, "verifier")
    assert verifier.get(f"/room/{sig_id}").status_code == 200

    text = report.report(session_factory())
    assert "verifier" not in text                 # a free read of a closed call is not a purchase
    assert "nothing waiting" in text              # the empty day before the first call never needs anchoring


def test_report_quiet_room(session_factory):
    text = report.report(session_factory())
    assert "no bot closed a call in this window" in text
    assert "nothing anchored yet" in text
    assert "price sampler is not running" in text


def test_weekly_report_has_a_day_by_day_line(client, session_factory):
    good = ApiAgent(client, "momentum-control")
    post_and_resolve(client, session_factory, good, 8, right=True, confidence=0.6)
    now = dt.datetime(2026, 11, 5, 12, 0)
    spread(session_factory, minutes=600, end=now - dt.timedelta(hours=1))   # 8 calls, 10 hours apart, over ~3 days
    text = report.report(session_factory(), now=now, days=7)
    assert "Signal Room, last 7 days" in text and "By day (right/closed, all bots):" in text
    assert text.count("\n  2026-11-0") >= 3 and "8 new calls, 8 closed: 8 right, 0 wrong" in text


def test_report_counts_priced_calls_against_their_cost(client, session_factory):
    from tests.test_priced import T0, rows_for
    a = ApiAgent(client, "favourite-control")
    rows_for(session_factory, a.id, 8, 2, bar=0.80, stated=0.80)          # ten calls in the two days before T0
    s = session_factory()
    s.add(Signal(agent_id=a.id, pair="BTC", horizon="15m", kind="priced", venue="kalshi", market="KXBTC15M-X-00", body_cipher="-",
                 hash="f" * 64, hash_version=4, submitted_at=T0 - dt.timedelta(days=2), resolve_at=T0 - dt.timedelta(days=2),
                 resolved_at=T0 - dt.timedelta(days=2) + dt.timedelta(minutes=14), fill="unfilled", result="unfilled", credit=0.0))
    s.commit()
    text = report.report(s, now=T0, days=7)
    assert "11 closed: 8 right, 2 wrong, 1 not filled (80% right; a coin flip is 50%; priced calls are judged against what they cost)" in text
    assert "right 80% (8 of 10), cost 80%, 1 not filled" in text
    s.close()


def test_a_yardstick_s_report_line_says_control(client, session_factory):
    good = ApiAgent(client, "momentum-control")
    post_and_resolve(client, session_factory, good, 33, right=True, confidence=0.8)
    now = dt.datetime(2026, 11, 4, 12, 0)
    spread(session_factory, end=now - dt.timedelta(hours=1))
    text = report.report(session_factory(), now=now)
    assert "verdict: Control (unchanged)" in text and "verdict moved" not in text

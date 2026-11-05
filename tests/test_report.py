"""`./start.sh report` must count the window exactly and say when a verdict moved."""
import datetime as dt

import pytest

from signal_room import config, report
from signal_room.db import Signal
from tests.test_api import Agent as ApiAgent
from tests.test_delisting import post_and_resolve


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 10_000)


def test_bar_marks_coin_flip():
    assert report.bar(0.5) == "██████████│··········"
    assert report.bar(0.75) == "██████████│█████·····"
    assert report.bar(0.0) == "··········│··········"


def test_report_counts_window_and_verdict_change(client, session_factory):
    good = ApiAgent(client, "momentum-control")
    post_and_resolve(client, session_factory, good, 30, right=True, confidence=0.8)
    post_and_resolve(client, session_factory, good, 3, right=False, confidence=0.8)

    # Spread the calls 15 minutes apart so each is its own try. The last 10 close inside the report window.
    now = dt.datetime(2026, 11, 4, 12, 0)
    db = session_factory()
    sigs = db.query(Signal).order_by(Signal.submitted_at).all()
    for i, sig in enumerate(sigs):
        sig.submitted_at = now - dt.timedelta(hours=48) + dt.timedelta(minutes=15 * i) if i < 23 \
            else now - dt.timedelta(hours=20) + dt.timedelta(minutes=15 * i)
        sig.resolved_at = sig.submitted_at + dt.timedelta(minutes=15)
    db.commit()

    text = report.report(session_factory(), now=now)
    assert "10 new calls, 10 closed: 7 right, 3 wrong (70% right" in text
    assert "Momentum\n  ██████████│████······  right 70% (7 of 10)" in text
    # Before the window: 23 scored, under the 30 needed, so Unproven. After: 33 scored.
    assert "verdict moved: Unproven →" in text

    day = report.report(session_factory(), day=dt.date(2026, 11, 2))
    assert "Signal Room, 2026-11-02 (UTC day)" in day


def test_report_quiet_room(session_factory):
    text = report.report(session_factory())
    assert "no bot closed a call in this window" in text
    assert "nothing anchored yet" in text
    assert "price sampler is not running" in text

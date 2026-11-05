"""A feed that is stale or wildly wrong must not move the reference price."""
import datetime as dt

from signal_room import prices
from signal_room.db import AuditEvent, ReferencePrice, utcnow


def test_outlier_feed_is_dropped():
    assert prices.good_feeds({"a": 100.0, "b": 100.2, "c": 100.1}) == {"a": 100.0, "b": 100.2, "c": 100.1}
    # c is 20% off: dropped; a and b stay.
    assert prices.good_feeds({"a": 100.0, "b": 100.2, "c": 120.0}) == {"a": 100.0, "b": 100.2}
    # Two feeds that disagree by more than 1%: neither can be trusted.
    assert prices.good_feeds({"a": 100.0, "b": 102.0}) == {}
    assert prices.good_feeds({"a": 100.0}) == {"a": 100.0}


def test_stale_and_outlier_feeds_are_dropped_and_logged(session_factory, monkeypatch):
    now = utcnow()
    monkeypatch.setattr(prices, "FEEDS", {
        "fresh1": lambda c, p: (100.0, now - dt.timedelta(seconds=5)),
        "fresh2": lambda c, p: (100.1, now - dt.timedelta(seconds=10)),
        "stale": lambda c, p: (90.0, now - dt.timedelta(minutes=20)),     # old quote from an outage
        "wrong": lambda c, p: (150.0, None),                               # live but far off
    })
    monkeypatch.setattr(prices.config, "PAIRS", ("BTC",))
    s = session_factory()
    med = prices.sample_all(s, client=object(), now=now.replace(second=0, microsecond=0))
    assert med == {"BTC": 100.05}                                           # median of the two good feeds
    row = s.query(ReferencePrice).first()
    assert row.feeds_used == 2
    events = {e.event for e in s.query(AuditEvent).all()}
    assert {"feed_stale", "feed_outlier", "price_degraded"} <= events

"""Findings from the independent review of rule set 4 (2026-11-11), each pinned so it cannot return.
The scoring findings are in test_account.py and test_rule_set_4.py, the checker's in test_verify.py."""
import datetime as dt
import threading

import pytest

from signal_room import config, db as dbmod, kalshi, scoring, service
from signal_room.db import Agent as AgentRow, CreditEntry, Signal
from tests.api_helpers import Agent
from tests.test_priced import NEXT_XRP, T0, XRP, call, clock, close_market, no_at_85, row  # noqa: F401


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 10_000)


def window(i):
    return f"KXXRP15M-W{i:05d}-00"


def run_windows(agent, venue, clock, n, wins, yes_bid=0.58, yes_ask=0.59, first=0, **body):
    """Post on n windows in a row, 3 s after each opens, so each call is settled by the next post."""
    for i in range(first, first + n + 1):
        opened = T0 + dt.timedelta(minutes=15 * i)
        venue.add_market(window(i), opened, yes_bid=yes_bid, yes_ask=yes_ask)
        if i > first:
            venue.settle(window(i - 1), "yes" if wins(i - 1) else "no")
        clock.t = opened + dt.timedelta(seconds=3)
        r = call(agent, market=window(i), side="yes", **body)
        assert r.status_code == 201 and r.json()["fill"] == "filled", r.text


@pytest.mark.filedb
def test_a_call_closed_by_two_threads_at_once_is_paid_once(client, session_factory, venue, clock, monkeypatch):
    """The minute tick and the bot's own next post both reach a closed call while its result is being
    fetched. Before the fix both paid its credits."""
    no_at_85(venue)
    a = Agent(client, "bot")
    sid = call(a, confidence=0.99).json()["signal_id"]           # NO at 85c, says 99%
    venue.settle(XRP, "no")
    venue.add_market(NEXT_XRP, T0 + dt.timedelta(minutes=15), yes_bid=0.15, yes_ask=0.16)
    clock.to(minutes=16, seconds=30)
    both_at_the_venue = threading.Barrier(2, timeout=5)
    real_fetch = kalshi.fetch_market

    def slow_fetch(ticker, *args, **kw):
        market = real_fetch(ticker, *args, **kw)
        if ticker == XRP and market.get("status") == "finalized":
            try:
                both_at_the_venue.wait()
            except threading.BrokenBarrierError:
                pass
        return market
    monkeypatch.setattr(kalshi, "fetch_market", slow_fetch)
    out = {}

    def tick():
        s = session_factory()
        out["tick"] = service.resolve_due(s, clock())
        s.close()

    def post():
        out["post"] = call(a, market=NEXT_XRP, confidence=0.99).status_code
    threads = [threading.Thread(target=tick), threading.Thread(target=post)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    s = session_factory()
    assert out["post"] == 201 and s.get(Signal, sid).result == "right"
    assert [e.amount for e in s.query(CreditEntry).filter_by(signal_id=sid, reason="resolved")] == [pytest.approx(0.078)]
    assert service.balance(s, a.id) == pytest.approx(config.STARTER_CREDITS + 0.078)
    s.close()


def test_a_call_settled_by_the_bot_s_next_post_still_counts_toward_delisting(client, session_factory, venue, clock):
    """Says 99% on 61c contracts and wins three in four: skill about -0.13, under the -0.10 line. Every call
    is settled by the next post, never by the tick; the standing check has to run there too."""
    a = Agent(client, "overclaimer")
    run_windows(a, venue, clock, 95, wins=lambda i: i % 4 != 0, limit=0.59, confidence=0.99)
    s = session_factory()
    assert s.get(AgentRow, a.id).status == "delisted"
    s.close()


def test_a_stated_chance_below_the_cost_is_scored_as_claiming_nothing(client, session_factory, venue, clock):
    """Ask 50c, cost 52c. Stating 50% used to pay +0.0816 on a loss and cost 0.0784 on a win: free credits
    for a bot that wins less often than its price. Now it is scored as if it had stated the cost."""
    for i, result in enumerate(("yes", "no")):
        ticker = window(i)
        venue.add_market(ticker, T0, yes_bid=0.49, yes_ask=0.50)
        a = Agent(client, f"bot{i}")
        r = call(a, market=ticker, side="yes", limit=0.50, confidence=0.50)
        assert r.json()["cost"] == 0.52
        close_market(venue, clock, session_factory, result, ticker)
        clock.to(seconds=90)
        sig = row(session_factory, r.json()["signal_id"])
        assert sig.credit == 0.0 and sig.confidence == 0.52 and sig.brier == pytest.approx((0.52 - (1 - i)) ** 2)
        mine = a.get("/me").json()["signals"][0]
        assert mine["stated"] == 0.50 and mine["confidence"] == 0.50      # what it said is still what is shown and hashed
        s = session_factory()
        assert s.query(CreditEntry).filter_by(signal_id=sig.id).count() == 0
        s.close()


def test_stating_more_than_the_cost_is_scored_as_stated(client, session_factory, venue, clock):
    venue.add_market(window(0), T0, yes_bid=0.49, yes_ask=0.50)
    r = call(Agent(client, "bot"), market=window(0), side="yes", limit=0.50, confidence=0.60)
    close_market(venue, clock, session_factory, "yes", window(0))
    sig = row(session_factory, r.json()["signal_id"])
    assert sig.confidence == 0.60 and sig.credit == pytest.approx(4 * (0.48 ** 2 - 0.40 ** 2), abs=1e-4)


def test_a_delisted_bot_cannot_come_back_on_calls_that_claim_nothing(client, session_factory, venue, clock):
    """A call with no stated chance is worth exactly zero. Rounding used to leave its skill a hair above
    zero half the time, and "above zero over 30 fresh calls" let a delisted bot straight back in."""
    a = Agent(client, "bot")
    s = session_factory()
    agent = s.get(AgentRow, a.id)
    agent.status, agent.delisted_at, agent.delist_count = "delisted", T0 - dt.timedelta(days=1), 1
    s.commit()
    s.close()
    # Odd prices on purpose: bars whose squares do not round cleanly.
    run_windows(a, venue, clock, 45, wins=lambda i: i % 3 != 0, yes_bid=0.913, yes_ask=0.917, limit=0.95, confidence=None)
    s = session_factory()
    fresh = s.query(Signal).filter(Signal.agent_id == a.id, Signal.result.in_(("right", "wrong"))).all()
    assert len(fresh) >= 30 and all(x.credit == 0.0 for x in fresh)
    assert abs(scoring.skill_score(*service._briers_and_refs(fresh))) < 1e-12
    assert s.get(AgentRow, a.id).status == "delisted"
    # Stating exactly the limit, which sits under the cost, is no way back either.
    venue.settle(window(45), "yes")
    run_windows(a, venue, clock, 45, wins=lambda i: True, yes_bid=0.58, yes_ask=0.59, first=46, limit=0.59, confidence=0.59)
    assert s.get(AgentRow, a.id).status == "delisted"
    s.close()


def test_a_stated_ten_or_twenty_percent_lands_in_one_calibration_band():
    """The stated chance is recovered from the Brier score; 0.1 came back as 0.0999… for wins and 0.1 for
    losses, so one honest band showed as two wildly miscalibrated ones."""
    hits = [1] * 60 + [0] * 540                                           # 10c long shots that win 10% of the time
    rec = scoring.record([scoring.brier(0.10, float(h)) for h in hits], ["right" if h else "wrong" for h in hits], [],
                         bars=[0.10] * 600, fades=[0.07] * 600)
    assert [(b["band"], b["calls"], b["actual"], b["judged"]) for b in rec.calibration] == [("10%-20%", 600, 0.1, "ok")]
    for stated in (0.1, 0.2, 0.3, 0.55, 0.7, 0.9):
        assert scoring.confidence_from(round((stated - 1) ** 2, 6), "right") == stated
        assert scoring.confidence_from(round(stated ** 2, 6), "wrong") == stated


def test_a_failed_statement_is_logged_without_its_values():
    assert dbmod.make_engine("sqlite://").hide_parameters


def test_sizes_and_prices_that_are_not_numbers_are_refused():
    for bad in ("inf", "1e400", "nan", True, None, "5,000"):
        with pytest.raises(kalshi.VenueError):
            kalshi._count(bad)
    for bad in ("nan", True, None, "1.0001"):
        with pytest.raises(kalshi.VenueError):
            kalshi._dollars(bad)


def test_both_sides_firing_is_called_worse_than_its_bars():
    """With prices, betting on an agent's calls and betting against them can both pay (long shots that
    win too often, favourites that lose too often). The cautious label wins, on purpose."""
    assert scoring.verdict(4000, 4000, 0.0, 0.0) == "worse_than_chance"

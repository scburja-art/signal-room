"""The two bots that make priced calls: the favourite control and the bridge from Kakalshi. Both are
run against the real room (through its signed API) with a stand-in Kalshi."""
import datetime as dt

import pytest

from agents import favourite_control as fav
from agents import kakalshi_adapter as bridge
from agents.client import SignalRoomError
from signal_room import config, kalshi, service
from signal_room.db import Signal
from signal_room.sealing import unseal
from tests.api_helpers import Agent
from tests.kalshi_fake import fixture
from tests.test_priced import T0, XRP, clock, close_market  # noqa: F401

W = int(T0.replace(tzinfo=dt.timezone.utc).timestamp())      # the window that opens at T0, in unix seconds


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 10_000)


class RoomClient:
    """What the bots call, pointed at the test server."""
    def __init__(self, agent):
        self.agent, self.agent_id = agent, agent.id

    def post_priced(self, market, side, limit, confidence=None, venue="kalshi"):
        body = {"venue": venue, "market": market, "side": side, "limit": limit}
        if confidence is not None:
            body["confidence"] = confidence
        r = self.agent.post("/signals/priced", body)
        if r.status_code >= 400:
            raise SignalRoomError(r.status_code, r.text)
        return r.json()


def sealed(row):
    return unseal(row.agent_id, row.body_cipher)


# ---------- the favourite control ----------

def test_pick_takes_the_side_that_costs_more():
    live = fixture("markets_open_2026-11-11.json")["markets"][0]      # YES offered at 51c, NO at 50c
    assert fav.pick(live) == ("yes", 0.51)
    assert fav.pick({**live, "yes_ask_dollars": "0.2900", "no_ask_dollars": "0.7200"}) == ("no", 0.72)
    assert fav.pick({**live, "yes_ask_dollars": "0.5000", "no_ask_dollars": "0.5000"}) is None     # no favourite
    assert fav.pick({**live, "yes_ask_dollars": "1.0000"}) is None                                 # nothing on offer


def test_each_window_runs_once_and_never_late():
    """Project rule: a bot on a schedule remembers the last slot it ran and never runs it twice."""
    assert fav.next_window(W + 30, None) == W                        # before its moment: this window
    assert fav.next_window(W + 95, W) == W + fav.WINDOW              # just ran it: the next window
    assert fav.next_window(W + 899, W) == W + fav.WINDOW             # still the same window: not again
    assert fav.next_window(W + 301, None) == W + fav.WINDOW          # woke too late: missed, not posted late
    assert fav.next_window(W + fav.WINDOW + 5, W) == W + fav.WINDOW  # the next window, not yet run


def test_the_control_buys_all_four_favourites_and_can_earn_nothing(client, session_factory, venue, clock):
    venue.add_market("KXBTC15M-26NOV111345-45", T0, yes_bid=0.69, yes_ask=0.70)      # YES is the favourite at 70c
    venue.add_market("KXETH15M-26NOV111345-45", T0, yes_bid=0.24, yes_ask=0.25)      # NO at 76c
    venue.add_market("KXSOL15M-26NOV111345-45", T0, yes_bid=0.50, yes_ask=0.50)      # 50c each: no favourite
    venue.add_market(XRP, T0, yes_bid=0.02, yes_ask=0.03)                             # NO at 98c: over the 95c ceiling
    bot = RoomClient(Agent(client, fav.NAME))
    assert fav.run_slot(bot, W, venue.client()) == 3
    s = session_factory()
    rows = {r.pair: r for r in s.query(Signal).all()}
    assert set(rows) == {"BTC", "ETH", "XRP"}
    assert (sealed(rows["BTC"])["side"], sealed(rows["BTC"])["limit"], sealed(rows["BTC"])["confidence"]) == ("yes", 0.72, None)
    assert (sealed(rows["ETH"])["side"], sealed(rows["ETH"])["limit"]) == ("no", 0.78)
    assert sealed(rows["XRP"])["limit"] == 0.95                                       # never bids above the ceiling
    assert (rows["BTC"].fill, sealed(rows["BTC"])["fill"]["bar"], rows["ETH"].fill, sealed(rows["ETH"])["fill"]["bar"]) == ("filled", 0.72, "filled", 0.78)
    assert (rows["XRP"].fill, sealed(rows["XRP"])["fill"]["reason"]) == ("unfilled", "price outside 5c-95c")
    s.close()
    for ticker, result in (("KXBTC15M-26NOV111345-45", "yes"), ("KXETH15M-26NOV111345-45", "yes"), ("KXSOL15M-26NOV111345-45", "no")):
        venue.settle(ticker, result)
    close_market(venue, clock, session_factory, "no")
    s = session_factory()
    rec = service.agent_record(s, bot.agent_id, None, "all")
    assert rec["n"] == 2 and rec["tries"] == 1 and rec["hit_rate"] == 0.5             # BTC won, ETH lost: one window, one try
    assert rec["bss"] == 0.0 and service.balance(s, bot.agent_id) == config.STARTER_CREDITS   # nothing earned, nothing lost
    s.close()


def test_the_control_survives_an_unreadable_kalshi(client, session_factory, venue, clock):
    venue.down = True
    assert fav.run_slot(RoomClient(Agent(client, fav.NAME)), W, venue.client()) == 0


# ---------- the bridge from Kakalshi ----------

def bet(i=1, at=None, ticker=XRP, side="no", price=85, **extra):
    return {"id": i, "timestamp": (at or T0 + dt.timedelta(seconds=88)).replace(tzinfo=dt.timezone.utc).isoformat(),
            "source": "crypto_first_minute", "strategy": "crypto_first_minute", "ticker": ticker, "side": side,
            "price_paid": price, "contracts": 2, "status": "pending", **extra}


def test_parse_bet_by_hand():
    assert bridge.parse_bet(bet()) == (XRP, "no", 0.85, 0.90)                 # limit: what it paid; states 5 points more
    assert bridge.parse_bet(bet(price=97)) == (XRP, "no", 0.97, 0.99)         # never states more than 99%
    assert bridge.parse_bet(bet(side="yes", price=38)) == (XRP, "yes", 0.38, 0.43)
    for other in (bet(ticker="KXHIGHNY-26NOV11-T55"), bet(ticker="KXBTC-26NOV1114-B74000"), bet(side="maybe"),
                  bet(price=None), bet(price=0), bet(price=100), bet(price="lots")):
        assert bridge.parse_bet(other) is None


def test_a_fresh_bet_becomes_a_priced_call_under_the_new_name(client, session_factory, venue, clock):
    venue.add_market(XRP, T0, yes_bid=0.15, yes_ask=0.16)                     # NO offered at 85c
    made = {}

    def client_for(strategy):
        made.setdefault(strategy, RoomClient(Agent(client, f"kakalshi-{strategy}-priced")))
        return made[strategy]

    posted, started = set(), T0.replace(tzinfo=dt.timezone.utc)
    now = (T0 + dt.timedelta(seconds=90)).replace(tzinfo=dt.timezone.utc)
    assert bridge.handle(bet(), now, started, posted, client_for) is False
    assert posted == {f"1@{bet()['timestamp']}"} and list(made) == ["crypto-first-minute"]
    s = session_factory()
    row = s.query(Signal).one()
    assert (row.kind, row.market, row.fill, row.bar) == ("priced", XRP, "filled", None)   # the cost stays sealed while open
    assert {k: sealed(row)[k] for k in ("side", "limit", "confidence")} == {"side": "no", "limit": 0.85, "confidence": 0.90}
    assert sealed(row)["fill"]["bar"] == 0.86
    s.close()
    assert bridge.handle(bet(), now, started, posted, client_for) is False    # already posted: nothing happens
    s = session_factory()
    assert s.query(Signal).count() == 1
    s.close()


def test_bets_the_bridge_must_not_post(client, session_factory, venue, clock):
    venue.add_market(XRP, T0, yes_bid=0.15, yes_ask=0.16)
    posts = []

    class Recorder:
        def post_priced(self, *a):
            posts.append(a)
            return {"fill": "filled", "signal_id": "x"}

    started = T0.replace(tzinfo=dt.timezone.utc)
    now = (T0 + dt.timedelta(seconds=90)).replace(tzinfo=dt.timezone.utc)
    posted = set()
    go = lambda b: bridge.handle(b, now, started, posted, lambda s: Recorder())
    assert go(bet(1, at=T0 - dt.timedelta(minutes=5))) is False              # made before the bridge started: history
    assert go(bet(2, at=T0 + dt.timedelta(seconds=50))) is False             # first seen 40s late: the price is gone
    assert go(bet(3, ticker="KXHIGHNY-26NOV11-T55")) is False                # not a market the room takes
    assert go({"id": 4, "timestamp": "not a time"}) is False
    assert posts == [] and len(posted) == 4                                  # each remembered, none posted
    assert go(bet(5)) is False and len(posts) == 1                           # a fresh one goes through


def test_a_transient_refusal_is_retried_and_a_permanent_one_is_not():
    started = T0.replace(tzinfo=dt.timezone.utc)
    now = (T0 + dt.timedelta(seconds=90)).replace(tzinfo=dt.timezone.utc)

    def refusing(status):
        class C:
            def post_priced(self, *a):
                raise SignalRoomError(status, "no")
        return lambda s: C()

    for status in (409, 429, 503):
        posted = set()
        assert bridge.handle(bet(), now, started, posted, refusing(status)) is True and posted == set()
    posted = set()
    assert bridge.handle(bet(), now, started, posted, refusing(422)) is False and len(posted) == 1

    class Broken:
        def post_priced(self, *a):
            raise ConnectionError("server restarting")
    posted = set()
    assert bridge.handle(bet(), now, started, posted, lambda s: Broken()) is True and posted == set()

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
    assert fav.next_window(W + 151, None) == W + fav.WINDOW          # woke too late: missed, not posted late
    assert fav.next_window(W + 100, None) == W                       # started ten seconds late: still this window
    assert fav.next_window(W + fav.WINDOW + 5, W) == W + fav.WINDOW  # the next window, not yet run


class FakeTime:
    """A clock the test moves. sleep() jumps it forward, by more than asked if the machine "slept"."""
    def __init__(self, now, oversleep=0.0):
        self.now, self.oversleep, self.slept = now, oversleep, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds + self.oversleep


def four_markets(venue):
    for coin in ("BTC", "ETH", "SOL", "XRP"):
        venue.add_market(f"KX{coin}15M-26NOV111345-45", T0, yes_bid=0.69, yes_ask=0.70)


def test_the_loop_runs_each_window_once_at_its_moment(client, session_factory, venue, clock):
    four_markets(venue)
    bot, t = RoomClient(Agent(client, fav.NAME)), FakeTime(W + 20)
    done = fav.step(bot, None, t, t.sleep)
    assert done == W and t.slept == [70] and t.now == W + 90           # waited for 90 s in, then ran
    s = session_factory()
    assert s.query(Signal).count() == 4
    done = fav.step(bot, done, t, t.sleep)                             # straight back in: the same window is not run again
    assert done == W + fav.WINDOW and t.now == W + fav.WINDOW + 90
    assert s.query(Signal).count() == 4                                # (the next window has no markets here: nothing new)
    s.close()


def test_a_machine_that_sleeps_through_the_moment_misses_the_window(client, session_factory, venue, clock):
    """Review finding 2026-11-11: the time was not read again after the wait, so a laptop that slept
    eleven minutes bought its favourites with two and a half minutes left."""
    four_markets(venue)
    bot, t = RoomClient(Agent(client, fav.NAME)), FakeTime(W + 20, oversleep=660)
    assert fav.step(bot, None, t, t.sleep) == W                        # the window counts as done
    s = session_factory()
    assert s.query(Signal).count() == 0                                # and nothing was posted late
    s.close()


def test_a_bot_started_mid_window_does_not_post_late(client, session_factory, venue, clock):
    four_markets(venue)
    bot = RoomClient(Agent(client, fav.NAME))
    assert fav.run_slot(bot, W, venue.client(), now=W + 299) == 0      # five minutes in: missed
    assert fav.run_slot(bot, W, venue.client(), now=W + 120) == 4      # thirty seconds late: still the same bet


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
    fills = {pair: sealed(r)["fill"] for pair, r in rows.items()}
    assert (fills["BTC"]["status"], fills["BTC"]["bar"], fills["ETH"]["status"], fills["ETH"]["bar"]) == ("filled", 0.72, "filled", 0.78)
    assert (fills["XRP"]["status"], fills["XRP"]["reason"]) == ("unfilled", "price outside 5c-95c")
    assert {r.fill for r in rows.values()} == {"decided"}                             # the open rows say only that
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
    assert (row.kind, row.market, row.fill, row.bar) == ("priced", XRP, "decided", None)   # fill and cost stay sealed while open
    assert {k: sealed(row)[k] for k in ("side", "limit", "confidence")} == {"side": "no", "limit": 0.85, "confidence": 0.90}
    assert (sealed(row)["fill"]["status"], sealed(row)["fill"]["bar"]) == ("filled", 0.86)
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


def test_each_bet_s_age_is_taken_when_its_turn_comes(client, session_factory, venue, clock):
    """Review finding 2026-11-11: the time was read once per look at the file, so four bets seen at 25 s,
    each taking 6 s to post, landed at 31, 37, 43 and 49 s against the 30-second rule."""
    started = T0.replace(tzinfo=dt.timezone.utc)
    t = [(T0 + dt.timedelta(seconds=88 + 25)).replace(tzinfo=dt.timezone.utc)]
    posts = []

    class Slow:
        def post_priced(self, *a):
            posts.append(a[0])
            t[0] += dt.timedelta(seconds=6)                                  # each post takes six seconds
            return {"fill": "filled", "signal_id": "x"}
    posted = set()
    coins = ("BTC", "ETH", "SOL", "XRP")
    for i, coin in enumerate(coins):
        bridge.handle(bet(i, ticker=f"KX{coin}15M-26NOV111345-45"), lambda: t[0], started, posted, lambda s: Slow())
    assert posts == ["KXBTC15M-26NOV111345-45"]                              # 25 s old: posted. 31, 37, 43 s: not.
    assert len(posted) == 4


def test_a_refused_bet_waits_before_it_is_tried_again():
    started = T0.replace(tzinfo=dt.timezone.utc)
    now = (T0 + dt.timedelta(seconds=90)).replace(tzinfo=dt.timezone.utc)
    tries = []

    class Busy:
        def post_priced(self, *a):
            tries.append(1)
            raise SignalRoomError(409, "one open signal per pair per horizon")
    posted, holdoff = set(), {}
    for second in range(12):                                                 # the file is looked at once a second
        assert bridge.handle(bet(), now + dt.timedelta(seconds=second), started, posted, lambda s: Busy(), holdoff) is True
    assert len(tries) == 3                                                   # at 0, 5 and 10 s: not twelve times


def test_a_shadow_log_of_an_unexpected_shape_is_read_as_empty(tmp_path):
    for text in ("[]", '{"bets": null}', '{"bets": 7}', "3", '"x"', '[{"id": 1}, 5]'):
        f = tmp_path / "shadow.json"
        f.write_text(text)
        assert bridge.load_bets(str(f)) == ([{"id": 1}] if "id" in text else [])


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


# ---------- second review, 2026-11-11 ----------

def test_slow_posts_do_not_carry_the_control_past_its_deadline(client, session_factory, venue, clock):
    """Each post takes 40 s. The time is looked at before every coin, so only the coins that can still be
    bought on time are; the rest are missed."""
    four_markets(venue)
    t = FakeTime(W + 90)

    class Slow(RoomClient):
        def post_priced(self, *a, **k):
            t.now += 40
            return super().post_priced(*a, **k)
    assert fav.run_slot(Slow(Agent(client, fav.NAME)), W, venue.client(), clock=t) == 2    # at 90 s and 130 s; 170 s is too late
    s = session_factory()
    assert s.query(Signal).count() == 2
    s.close()


def test_a_timeout_on_one_coin_does_not_cost_the_others_their_turn(client, session_factory, venue, clock):
    four_markets(venue)
    calls = []

    class Flaky(RoomClient):
        def post_priced(self, market, *a, **k):
            calls.append(market)
            if "BTC" in market:
                raise TimeoutError("no answer")
            return super().post_priced(market, *a, **k)
    assert fav.run_slot(Flaky(Agent(client, fav.NAME)), W, venue.client(), now=W + 90) == 3 and len(calls) == 4


def test_the_bridge_survives_odd_bets_and_odd_state(tmp_path):
    started = T0.replace(tzinfo=dt.timezone.utc)
    now = (T0 + dt.timedelta(seconds=90)).replace(tzinfo=dt.timezone.utc)
    posted, posts = set(), []

    class Recorder:
        def post_priced(self, *a):
            posts.append(a)
            return {"fill": "filled", "signal_id": "x"}
    go = lambda b: bridge.handle(b, now, started, posted, lambda s: Recorder())
    assert go(bet(1, ticker=12345)) is False and go(bet(2, ticker=["KXBTC15M"])) is False     # a ticker that is not text
    assert go(bet(3, at=T0 + dt.timedelta(minutes=10))) is False                              # stamped ten minutes in the future
    assert posts == [] and len(posted) == 3
    f = tmp_path / "posted.json"
    for text in ("7", "[[1, 2], [3]]", '{"a": 1}', "not json"):
        f.write_text(text)
        assert isinstance(bridge.load_posted(str(f)), set)


def test_the_wait_before_a_retry_runs_from_the_end_of_the_failed_try():
    started = T0.replace(tzinfo=dt.timezone.utc)
    t = [(T0 + dt.timedelta(seconds=90)).replace(tzinfo=dt.timezone.utc)]

    class Hangs:
        def post_priced(self, *a):
            t[0] += dt.timedelta(seconds=15)                                 # the post hangs 15 s, then fails
            raise ConnectionError("no answer")
    posted, holdoff = set(), {}
    assert bridge.handle(bet(), lambda: t[0], started, posted, lambda s: Hangs(), holdoff) is True
    (until,) = holdoff.values()
    assert until == t[0] + dt.timedelta(seconds=bridge.RETRY_SECONDS)        # five seconds after the failure, not before it

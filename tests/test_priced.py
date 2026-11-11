"""Priced calls end to end: posted through the signed API, filled against what the room reads at the
venue, settled by the venue's own result, scored against what they cost. The venue is a stand-in
serving Kalshi's real shapes; no test touches the network."""
import datetime as dt

import pytest

from signal_room import config, kalshi, scoring, service
from signal_room.db import Agent as AgentRow, AuditEvent, CreditEntry, Signal
from signal_room.sealing import priced_hash, priced_leaf
from tests.api_helpers import Agent

T0 = dt.datetime(2026, 11, 11, 18, 30)          # a 15-minute window opens
XRP = "KXXRP15M-26NOV111345-45"
BTC = "KXBTC15M-26NOV111345-45"
NEXT_XRP = "KXXRP15M-26NOV111400-00"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def to(self, **after_t0):
        self.t = T0 + dt.timedelta(**after_t0)
        return self.t


@pytest.fixture()
def clock(monkeypatch):
    """The room's clock, set 90 seconds into the window: about when Kakalshi's first-minute bets land."""
    c = Clock(T0 + dt.timedelta(seconds=90))
    monkeypatch.setattr(service, "utcnow", c)
    return c


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 10_000)


def no_at_85(venue, ticker=XRP, opened=T0):
    """A market where NO is offered at 85c and bid at 84c (YES: bid 15c, ask 16c)."""
    return venue.add_market(ticker, opened, yes_bid=0.15, yes_ask=0.16)


def call(agent, market=XRP, side="no", limit=0.85, confidence=0.90, **extra):
    body = {"market": market, "side": side, "limit": limit, **extra}
    if confidence is not None:
        body["confidence"] = confidence
    return agent.post("/signals/priced", body)


def close_market(venue, clock, session_factory, result, ticker=XRP):
    venue.settle(ticker, result)
    clock.to(minutes=15, seconds=5)
    s = session_factory()
    n = service.resolve_due(s)
    s.close()
    return n


def row(session_factory, signal_id):
    s = session_factory()
    try:
        return s.get(Signal, signal_id)
    finally:
        s.close()


# ---------- the worked examples, through the room ----------

def test_example_1_a_priced_call_that_wins(client, session_factory, venue, clock):
    no_at_85(venue)
    a = Agent(client, "kakalshi-crypto-first-minute-priced")
    r = call(a)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["fill"] == "filled" and out["price"] == 0.85 and out["cost"] == 0.86 and out["hash_version"] == 4
    assert out["resolve_at"] == (T0 + dt.timedelta(minutes=15)).isoformat()      # closes when its market does

    assert close_market(venue, clock, session_factory, "no") == 1
    sig = row(session_factory, out["signal_id"])
    assert (sig.result, sig.venue_result, sig.side, sig.direction) == ("right", "no", "no", "short")
    assert sig.bar == 0.86 and sig.fade_bar == 0.83 and sig.fill_source == "live" and sig.fill_size == 5000.0
    assert sig.brier == pytest.approx(0.01) and sig.credit == pytest.approx(0.0384)   # 4 x (0.14^2 - 0.10^2)
    assert sig.net_return == pytest.approx(0.14)                                 # 14c profit on the contract
    assert sig.scoring_version == scoring.SCORING_VERSION == 4
    assert sig.resolved_at == T0 + dt.timedelta(minutes=15) and sig.start_at == T0 + dt.timedelta(seconds=90)
    s = session_factory()
    assert s.query(CreditEntry).filter_by(signal_id=sig.id, reason="resolved").one().amount == pytest.approx(0.0384)
    rec = service.agent_record(s, a.id, None, "all")
    assert rec["n"] == 1 and rec["priced"] == 1 and rec["bar"] == 0.86 and rec["edge"] == pytest.approx(0.14)
    assert rec["profit_per_contract"] == pytest.approx(0.14)
    s.close()


def test_example_2_the_same_call_had_it_lost(client, session_factory, venue, clock):
    no_at_85(venue)
    a = Agent(client, "bot")
    sid = call(a).json()["signal_id"]
    close_market(venue, clock, session_factory, "yes")
    sig = row(session_factory, sid)
    assert sig.result == "wrong" and sig.credit == pytest.approx(-0.2816) and sig.net_return == pytest.approx(-0.86)


def test_example_6_not_filled_is_recorded_and_never_scored(client, session_factory, venue, clock):
    no_at_85(venue)
    venue.quote(XRP, yes_bid=0.12, yes_ask=0.13)          # NO now costs 88c
    a = Agent(client, "bot")
    out = call(a, limit=0.85).json()
    assert out["fill"] == "unfilled" and out["fill_reason"] == "price above the limit" and out["cost"] is None
    sig = row(session_factory, out["signal_id"])
    assert sig.resolved_at is None and sig.side is None    # still sealed while its market is open
    assert sig.fill_ask == 0.88 and sig.bar is None
    close_market(venue, clock, session_factory, "no")      # it would have won; it is still not scored
    sig = row(session_factory, out["signal_id"])
    assert sig.result == "unfilled" and sig.credit == 0.0 and sig.brier is None and sig.side == "no"
    s = session_factory()
    assert service.agent_record(s, a.id, None, "all")["n"] == 0
    assert s.query(CreditEntry).filter_by(signal_id=sig.id).count() == 0
    s.close()


@pytest.mark.parametrize("setup,reason", [
    (lambda v, c: v.quote(XRP, 0.15, 0.16, size=9.0), "under 10 on offer"),
    (lambda v, c: v.quote(XRP, 0.03, 0.04), "price outside 5c-95c"),        # NO at 97c
    (lambda v, c: v.quote(XRP, 0.0, 0.16), "nothing on offer"),              # nobody selling NO
    (lambda v, c: c.to(minutes=14, seconds=30), "under a minute left"),
])
def test_every_fill_limit(client, session_factory, venue, clock, setup, reason):
    no_at_85(venue)
    setup(venue, clock)
    out = call(Agent(client, "bot"), limit=0.99, confidence=0.99).json()
    assert (out["fill"], out["fill_reason"]) == ("unfilled", reason)


def test_decide_fill_by_hand():
    assert service.decide_fill(0.85, 10.0, 0.85, 60.0) == ("filled", "")       # every limit exactly met
    assert service.decide_fill(0.8501, 10.0, 0.85, 600.0)[0] == "unfilled"
    assert service.decide_fill(0.85, 9.99, 0.85, 600.0)[0] == "unfilled"
    assert service.decide_fill(0.85, 10.0, 0.85, 59.9)[0] == "unfilled"
    assert service.decide_fill(0.05, 10.0, 0.5, 600.0)[0] == "filled" and service.decide_fill(0.049, 10.0, 0.5, 600.0)[0] == "unfilled"
    assert service.decide_fill(0.95, 10.0, 0.99, 600.0)[0] == "filled" and service.decide_fill(0.951, 10.0, 0.99, 600.0)[0] == "unfilled"
    assert service.decide_fill(None, 10.0, 0.99, 600.0) == ("unfilled", "nothing on offer")


def test_example_9_a_call_that_claims_nothing_earns_nothing(client, session_factory, venue, clock):
    """The control bot's kind of call: no stated chance, so it is scored as saying exactly what it paid."""
    no_at_85(venue)
    no_at_85(venue, BTC)
    a = Agent(client, "favourite-control")
    won = call(a, confidence=None).json()["signal_id"]
    lost = call(a, market=BTC, confidence=None).json()["signal_id"]
    venue.settle(BTC, "yes")
    close_market(venue, clock, session_factory, "no")
    w, l = row(session_factory, won), row(session_factory, lost)
    assert (w.result, l.result) == ("right", "wrong") and w.credit == 0.0 and l.credit == 0.0
    assert w.confidence == 0.86 == l.confidence
    s = session_factory()
    rec = service.agent_record(s, a.id, None, "all")
    assert rec["bss"] == 0.0 and rec["tries"] == 1          # two coins, one window, one try
    assert s.query(CreditEntry).filter_by(agent_id=a.id, reason="resolved").count() == 0
    s.close()


# ---------- sealed while open ----------

def test_nothing_about_an_open_priced_call_gives_its_side_away(client, session_factory, venue, clock, caplog):
    no_at_85(venue)
    author, other = Agent(client, "author"), Agent(client, "other")
    with caplog.at_level("INFO", logger="signal_room.service"):
        sid = call(author).json()["signal_id"]
    listing = other.get("/room").json()["signals"]
    assert len(listing) == 1
    item = listing[0]
    assert (item["kind"], item["venue"], item["market"], item["pair"], item["horizon"]) == ("priced", "kalshi", XRP, "XRP", "15m")
    day = other.get(f"/log/{clock().date().isoformat()}").json()["signals"][0]
    secrets = ("side", "limit", "fill", "price", "cost", "bar", "ask", "bid", "direction", "confidence", "stated")
    for public in (item, day):
        assert not any(k in public for k in secrets), public
    assert day["start_at"] is None and day["leaf"] != day["hash"]
    line = next(r.getMessage() for r in caplog.records if "EVENT" in r.getMessage())
    assert '"kind": "priced"' in line and '"fill": "filled"' in line
    assert not any(w in line for w in ('"side"', "0.85", "0.86", '"no"'))
    # Its author sees its own call in full; a paying reader does too.
    mine = author.get("/me").json()["signals"][0]
    assert mine["side"] == "no" and mine["limit"] == 0.85 and mine["fill"]["bar"] == 0.86
    paid = other.get(f"/room/{sid}").json()
    assert paid["side"] == "no" and paid["salt"] is None and paid["fill"]["ask"] == 0.85


def test_receipt_and_leaf_can_be_recomputed_once_closed(client, session_factory, venue, clock):
    no_at_85(venue)
    a, checker = Agent(client, "bot"), Agent(client, "checker")
    out = call(a).json()
    close_market(venue, clock, session_factory, "no")
    body = checker.get(f"/room/{out['signal_id']}").json()          # closed: free, and the salt is public
    assert body["result"] == "right" and body["venue_result"] == "no" and body["salt"]
    assert priced_hash(body["agent_id"], body["venue"], body["market"], body["side"], body["limit"], body["stated"],
                       body["submitted_at"], body["salt"]) == out["hash"] == body["hash"]
    listed = checker.get(f"/log/{T0.date().isoformat()}").json()["signals"][0]
    assert priced_leaf(body["hash"], body["salt"], body["fill"]) == listed["leaf"] == body["leaf"]
    assert body["fill"] == {"fill": "filled", "reason": "", "at": (T0 + dt.timedelta(seconds=90)).isoformat(), "ask": 0.85,
                            "bid": 0.84, "size": 5000.0, "source": "live", "bar": 0.86, "fade_bar": 0.83}


# ---------- what the room refuses ----------

def test_validation(client, session_factory, venue, clock):
    no_at_85(venue)
    a = Agent(client, "bot")
    assert call(a, venue="polymarket").status_code == 422
    assert call(a, market="KXDOGE15M-26NOV111345-45").status_code == 422      # not one of the room's coins
    assert call(a, market="KXBTC-26NOV1114-B74000").status_code == 422        # not a 15-minute market
    assert call(a, side="up").status_code == 422
    assert call(a, limit=0.0).status_code == 422 and call(a, limit=1.0).status_code == 422
    assert call(a, limit=0.85, confidence=0.80).status_code == 422            # sure of less than it would pay
    assert call(a, confidence=1.01).status_code == 422
    assert call(a, market="KXXRP15M-26NOV111500-00").status_code == 422       # Kalshi has no such market
    venue.settle(XRP, "no")
    assert call(a).status_code == 422                                         # that market is not open
    s = session_factory()
    assert s.query(Signal).count() == 0
    s.close()


def test_a_long_shot_may_state_a_chance_under_half(client, session_factory, venue, clock):
    venue.add_market(XRP, T0, yes_bid=0.29, yes_ask=0.30)
    r = call(Agent(client, "bot"), side="yes", limit=0.30, confidence=0.36)
    assert r.status_code == 201 and r.json()["cost"] == 0.32           # 30c + 2c fee
    close_market(venue, clock, session_factory, "yes")
    sig = row(session_factory, r.json()["signal_id"])
    assert sig.result == "right" and sig.direction == "long" and sig.credit == pytest.approx(4 * (0.68 ** 2 - 0.64 ** 2))


def test_retired_wallets_cannot_post_priced_calls(client, session_factory, venue, clock):
    no_at_85(venue)
    a = Agent(client, "bot")
    s = session_factory()
    s.get(AgentRow, a.id).status = "retired"
    s.commit()
    s.close()
    assert call(a).status_code == 403


# ---------- one open call per coin, and the copy rule ----------

def test_the_next_window_is_never_blocked_by_the_last_one(client, session_factory, venue, clock):
    no_at_85(venue)
    a = Agent(client, "bot")
    first = call(a).json()["signal_id"]
    assert call(a).status_code == 409                                  # one open call per coin per window
    assert a.post("/signals", {"pair": "XRP", "horizon": "15m", "direction": "long", "confidence": 0.6}).status_code == 409
    # The window ends and Kalshi finalises it, but the room's minute tick has not run yet.
    venue.settle(XRP, "no")
    venue.add_market(NEXT_XRP, T0 + dt.timedelta(minutes=15), yes_bid=0.15, yes_ask=0.16)
    clock.to(minutes=16, seconds=30)
    r = call(a, market=NEXT_XRP)
    assert r.status_code == 201 and r.json()["fill"] == "filled"       # the last call was settled on the spot
    assert row(session_factory, first).result == "right"


def test_copy_rule_covers_priced_calls(client, session_factory, venue, clock):
    no_at_85(venue)
    no_at_85(venue, BTC)
    author, reader = Agent(client, "author"), Agent(client, "reader")
    sid = call(author).json()["signal_id"]
    assert reader.get(f"/room/{sid}").status_code == 200               # paid for the open XRP call
    assert call(reader).status_code == 409                             # so no XRP call of its own, priced
    assert reader.post("/signals", {"pair": "XRP", "horizon": "1h", "direction": "short", "confidence": 0.6}).status_code == 409
    assert call(reader, market=BTC).status_code == 201                 # another coin is fine


# ---------- the room could not look in time ----------

def test_example_7_fill_from_the_worst_price_of_the_minute(client, session_factory, venue, clock):
    no_at_85(venue)
    a = Agent(client, "bot")
    venue.down = True
    first = call(a, limit=0.85)
    assert first.status_code == 201 and first.json()["fill"] == "waiting" and first.json()["cost"] is None
    s = session_factory()
    assert not service.day_ready_to_anchor(s, T0.date())               # no anchoring a day with an undecided fill
    assert service.fill_waiting(s, clock()) == 0                       # Kalshi is still down
    venue.down = False
    assert service.fill_waiting(s, clock()) == 0                       # that minute's record is not complete yet
    # In that minute the YES bid ran from 7c to 36c, so NO was offered from 64c up to 93c.
    venue.add_minute(XRP, T0 + dt.timedelta(minutes=2), yes_bid=(0.07, 0.36), yes_ask=(0.09, 0.39), volume=20333.0)
    assert service.fill_waiting(s, clock.to(minutes=3)) == 1
    sig = s.get(Signal, first.json()["signal_id"])
    assert (sig.fill, sig.fill_reason, sig.fill_source, sig.fill_ask) == ("unfilled", "price above the limit", "history", 0.93)
    assert sig.resolve_at == T0 + dt.timedelta(minutes=15) and service.day_ready_to_anchor(s, T0.date())
    s.close()


def test_history_fill_is_scored_at_the_worst_price(client, session_factory, venue, clock):
    no_at_85(venue)
    a = Agent(client, "bot")
    venue.down = True
    sid = call(a, limit=0.95, confidence=0.97).json()["signal_id"]
    venue.down = False
    venue.add_minute(XRP, T0 + dt.timedelta(minutes=2), yes_bid=(0.07, 0.36), yes_ask=(0.09, 0.39), volume=20333.0)
    close_market(venue, clock, session_factory, "no")                  # the fill is decided on the way to settling
    sig = row(session_factory, sid)
    assert sig.fill == "filled" and sig.fill_source == "history" and sig.fill_ask == 0.93
    assert sig.bar == 0.94 and sig.fill_at == sig.submitted_at and sig.result == "right"
    assert sig.fill_bid == pytest.approx(0.61)                         # 1 - the highest YES ask that minute
    assert sig.credit == pytest.approx(4 * (0.06 ** 2 - 0.03 ** 2))


def test_history_with_almost_nothing_traded_does_not_fill(client, session_factory, venue, clock):
    no_at_85(venue)
    venue.down = True
    sid = call(Agent(client, "bot"), limit=0.95, confidence=0.97).json()["signal_id"]
    venue.down = False
    venue.add_minute(XRP, T0 + dt.timedelta(minutes=2), yes_bid=(0.14, 0.16), yes_ask=(0.15, 0.17), volume=4.0)
    s = session_factory()
    assert service.fill_waiting(s, clock.to(minutes=3)) == 1
    assert (s.get(Signal, sid).fill, s.get(Signal, sid).fill_reason) == ("unfilled", "under 10 on offer")
    s.close()


def test_a_changed_fee_rule_stops_fills(client, session_factory, venue, clock):
    no_at_85(venue)
    venue.series["KXXRP15M"]["fee_multiplier"] = 2                     # Kalshi doubled this series' fee
    out = call(Agent(client, "bot")).json()
    assert out["fill"] == "waiting"                                    # never filled at a fee the room does not know
    s = session_factory()
    venue.add_minute(XRP, T0 + dt.timedelta(minutes=2), yes_bid=(0.14, 0.16), yes_ask=(0.15, 0.17))
    assert service.fill_waiting(s, clock.to(minutes=3)) == 0
    s.close()


def test_a_market_that_never_existed_is_closed_out_unfilled(client, session_factory, venue, clock):
    venue.down = True
    sid = call(Agent(client, "bot"), market="KXXRP15M-26NOV111500-00").json()["signal_id"]   # cannot be checked right now
    venue.down = False
    s = session_factory()
    assert service.fill_waiting(s, clock.to(minutes=3)) == 1
    assert (s.get(Signal, sid).fill, s.get(Signal, sid).fill_reason) == ("unfilled", "no such market")
    service.resolve_due(s, clock.to(minutes=20))
    assert s.get(Signal, sid).result == "unfilled"
    s.close()


def test_void_after_72_hours_without_a_price_or_a_result(client, session_factory, venue, clock):
    no_at_85(venue)
    no_at_85(venue, BTC)
    a = Agent(client, "bot")
    venue.down = True
    unread = call(a).json()["signal_id"]                               # never gets a price
    venue.down = False
    filled = call(a, market=BTC).json()["signal_id"]                   # filled, but Kalshi never gives a result
    s = session_factory()
    assert service.resolve_due(s, clock.to(hours=71)) == 0             # still waiting: a real call is not thrown away early
    assert s.get(Signal, unread).resolved_at is None and s.get(Signal, filled).resolved_at is None
    assert service.resolve_due(s, clock.to(hours=73)) == 2
    assert s.get(Signal, unread).result == "void" and s.get(Signal, filled).result == "void"
    assert s.get(Signal, filled).credit == 0.0 and s.query(CreditEntry).filter_by(reason="resolved").count() == 0
    assert s.query(AuditEvent).filter_by(event="signal_void").count() == 2
    s.close()


def test_a_result_that_is_not_yes_or_no_is_never_scored(client, session_factory, venue, clock):
    no_at_85(venue)
    sid = call(Agent(client, "bot")).json()["signal_id"]
    venue.settle(XRP, "scalar")
    s = session_factory()
    assert service.resolve_due(s, clock.to(minutes=16)) == 0
    assert service.resolve_due(s, clock.to(hours=73)) == 1 and s.get(Signal, sid).result == "void"
    s.close()


def test_a_passing_error_while_settling_leaves_the_call_for_the_next_tick(client, session_factory, venue, clock, monkeypatch):
    no_at_85(venue)
    sid = call(Agent(client, "bot")).json()["signal_id"]
    venue.settle(XRP, "no")
    s = session_factory()
    real = kalshi.result
    monkeypatch.setattr(kalshi, "result", lambda m: 1 / 0)             # a bug, not a venue problem
    assert service.resolve_due(s, clock.to(minutes=16)) == 0
    assert s.get(Signal, sid).resolved_at is None and s.get(Signal, sid).side is None   # not voided, still sealed
    monkeypatch.setattr(kalshi, "result", real)
    assert service.resolve_due(s, clock.to(minutes=17)) == 1 and s.get(Signal, sid).result == "right"
    s.close()


# ---------- standing ----------

def rows_for(session_factory, agent_id, n_right, n_wrong, bar, stated):
    """Scored priced calls written straight to the table, 15 minutes apart, newest last."""
    s = session_factory()
    base = T0 - dt.timedelta(days=2)
    for i in range(n_right + n_wrong):
        y = 1.0 if i % (n_right + n_wrong) < n_right else 0.0
        t = base + dt.timedelta(minutes=15 * i)
        s.add(Signal(agent_id=agent_id, pair="XRP", horizon="15m", kind="priced", venue="kalshi", market=f"KXXRP15M-X{i:05d}-00",
                     body_cipher="-", hash=f"{i:064x}", hash_version=4, submitted_at=t, start_at=t,
                     resolve_at=t + dt.timedelta(minutes=13), resolved_at=t + dt.timedelta(minutes=13),
                     fill="filled", bar=bar, fade_bar=bar - 0.03, side="no", confidence=stated,
                     result="right" if y else "wrong", brier=round((stated - y) ** 2, 6),
                     credit=round(scoring.credit_for((stated - y) ** 2, "right" if y else "wrong", bar), 4),
                     net_return=round(y - bar, 6), scoring_version=4))
    s.commit()
    s.close()


def test_delisting_judges_a_priced_record_against_its_bar(client, session_factory):
    """Says 99% on 60c contracts and wins three in four. Against a coin flip that looks fine (skill
    about +0.04); against what it paid it is badly overclaimed (about -0.12), and it is delisted."""
    a, honest = Agent(client, "overclaimer"), Agent(client, "honest")
    rows_for(session_factory, a.id, 68, 22, bar=0.60, stated=0.99)
    rows_for(session_factory, honest.id, 68, 22, bar=0.60, stated=0.70)
    s = session_factory()
    service.update_standing(s, {a.id, honest.id}, T0)
    assert s.get(AgentRow, a.id).status == "delisted" and s.get(AgentRow, honest.id).status == "active"
    briers = [x.brier for x in s.query(Signal).filter_by(agent_id=a.id)]
    assert not scoring.should_delist(briers)                           # the old, coin-flip bar would have kept it
    s.close()


def test_a_priced_record_reads_against_its_bar(client, session_factory):
    a = Agent(client, "edge")
    rows_for(session_factory, a.id, 75, 15, bar=0.70, stated=0.80)     # wins 83% of 90 at a 70c cost
    s = session_factory()
    rec = service.agent_record(s, a.id, None, "all")
    assert rec["priced"] == 90 and rec["bar"] == 0.70 and rec["hit_rate"] == pytest.approx(0.8333, abs=1e-4)
    assert rec["edge"] == pytest.approx(0.1333, abs=1e-4) and rec["edge_low"] < rec["edge"] < rec["edge_high"]
    assert rec["profit_per_contract"] == pytest.approx(0.1333, abs=1e-4)
    assert rec["cum_return"] == 0.0 and rec["always_long_return"] == 0.0    # bought, not traded 1:1
    assert rec["p_luck"] < 0.05 and rec["verdict"] == "probably_not_luck"
    assert service.leaderboard(s, None, None, "all")[0]["agent_id"] == a.id
    s.close()

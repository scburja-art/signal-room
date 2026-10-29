"""Regression tests for the cold-review findings and the behaviors it flagged as untested."""
import datetime as dt
import hashlib
import itertools
import json
import secrets
import threading
import time

import pytest

from signal_room import auth, config, scoring, service
from signal_room.db import ReferencePrice, Signal
from signal_room.sealing import canonical, merkle_root, sha256_hex
from tests.conftest import set_price
from tests.test_api import Agent


def test_hash_cannot_be_brute_forced_from_public_fields(client, session_factory):
    """Everything in the listing plus the public reference price must not be enough to recover the body."""
    a = Agent(client, "victim")
    set_price(session_factory, "BTC", 100.0)
    r = a.post("/signals", {"pair": "BTC", "horizon": "4h", "direction": "short", "confidence": 0.8731})
    assert r.status_code == 201
    listing = Agent(client, "attacker").get("/room").json()["signals"][0]
    # Attacker knows agent, pair, horizon, submitted_at, price (public). Tries every direction/confidence.
    found = None
    for direction, c in itertools.product(("long", "short"), range(5000, 10001)):
        guess = sha256_hex(canonical({
            "agent": listing["agent_id"], "pair": "BTC", "horizon": "4h", "direction": direction,
            "confidence": round(c / 10000, 4), "submitted_at": listing["submitted_at"], "price_at_submit": 100.0,
        }))
        if guess == listing["hash"]:
            found = (direction, c)
            break
    assert found is None


def test_hash_verifiable_after_resolve(client, session_factory):
    """Once resolved, the owner can reproduce the hash from the revealed body + salt."""
    from signal_room.sealing import signal_hash, unseal
    a = Agent(client, "v")
    set_price(session_factory, "ETH", 10.0)
    sid = a.post("/signals", {"pair": "ETH", "horizon": "15m", "direction": "long", "confidence": 0.6}).json()["signal_id"]
    s = session_factory()
    sig = s.query(Signal).filter_by(id=sid).first()
    body = unseal(sig.agent_id, sig.body_cipher)
    assert "salt" in body and len(body["salt"]) == 32
    assert signal_hash(sig.agent_id, "ETH", "15m", "long", 0.6, sig.submitted_at.isoformat(), 10.0, body["salt"]) == sig.hash
    s.close()


@pytest.mark.filedb
def test_concurrent_reads_cannot_overspend(client, session_factory, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 10_000)
    seller = Agent(client, "s")
    reader = Agent(client, "r")
    for p, v in (("BTC", 1.0), ("ETH", 1.0), ("SOL", 1.0), ("XRP", 1.0)):
        set_price(session_factory, p, v)
    ids = []
    for pair in ("BTC", "ETH", "SOL", "XRP"):
        for h in ("15m", "1h", "4h"):
            ids.append(seller.post("/signals", {"pair": pair, "horizon": h, "direction": "long", "confidence": 0.7}).json()["signal_id"])
    assert len(ids) == 12 and reader.get("/me").json()["credits"] == 10.0
    # pre-sign 12 reads, fire them together
    reqs = [(f"/room/{sid}", reader.headers("GET", f"/room/{sid}")) for sid in ids]
    results = []
    def go(path, h):
        results.append(client.get(path, headers=h).status_code)
    threads = [threading.Thread(target=go, args=r) for r in reqs]
    for t in threads: t.start()
    for t in threads: t.join()
    assert results.count(200) == 10 and results.count(402) == 2
    assert reader.get("/me").json()["credits"] == 0.0


def test_one_open_signal_enforced_by_database(session_factory):
    """Two rows for the same agent/pair/horizon with resolved_at NULL must be impossible."""
    from sqlalchemy.exc import IntegrityError
    s = session_factory()
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    s.add(Signal(agent_id="x", pair="BTC", horizon="1h", body_cipher="c", hash="h1", submitted_at=now,
                 resolve_at=now, price_at_submit=1.0))
    s.commit()
    s.add(Signal(agent_id="x", pair="BTC", horizon="1h", body_cipher="c", hash="h2", submitted_at=now,
                 resolve_at=now, price_at_submit=1.0))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_rate_limit_returns_429_before_signature_work(client, session_factory, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 5)
    a = Agent(client, "fast")
    codes = [a.get("/me").status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    # a stolen API key with a garbage signature is also throttled, not just rejected
    bad = {"X-Agent-Key": a.key, "X-Timestamp": str(int(time.time())), "X-Nonce": secrets.token_hex(8), "X-Signature": "0x" + "00" * 65}
    assert client.get("/me", headers=bad).status_code == 429


def test_stale_price_gives_503(client, session_factory):
    a = Agent(client, "s")
    set_price(session_factory, "SOL", 50.0, when=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=10))
    assert a.post("/signals", {"pair": "SOL", "horizon": "1h", "direction": "long", "confidence": 0.6}).status_code == 503


def test_void_flat_and_resolution_waits_for_post_horizon_sample(client, session_factory, monkeypatch):
    from signal_room import prices
    monkeypatch.setattr(prices, "CANDLE_FEEDS", {})  # no exchange history available in this test
    a = Agent(client, "s")
    set_price(session_factory, "XRP", 1.0)
    sid = a.post("/signals", {"pair": "XRP", "horizon": "15m", "direction": "long", "confidence": 0.7}).json()["signal_id"]
    s = session_factory()
    sig = s.query(Signal).filter_by(id=sid).first()
    # Only a pre-horizon sample exists and we are inside tolerance: wait, do not resolve.
    set_price(session_factory, "XRP", 1.5, when=sig.resolve_at - dt.timedelta(seconds=30))
    assert service.resolve_due(s, now=sig.resolve_at + dt.timedelta(seconds=5)) == 0
    # A sample at the horizon arrives, inside the flat band: resolves flat with zero credit.
    set_price(session_factory, "XRP", 1.0002, when=sig.resolve_at)
    assert service.resolve_due(s, now=sig.resolve_at + dt.timedelta(seconds=10)) == 1
    s.refresh(sig)
    assert sig.result == "flat" and sig.credit == 0.0 and sig.resolved_at == sig.resolve_at
    # Void: a signal with no price anywhere near its horizon, and no exchange history to rebuild it from.
    s.query(ReferencePrice).delete(); s.commit()
    set_price(session_factory, "XRP", 1.0)
    sid2 = a.post("/signals", {"pair": "XRP", "horizon": "15m", "direction": "long", "confidence": 0.7}).json()["signal_id"]
    sig2 = s.query(Signal).filter_by(id=sid2).first()
    s.query(ReferencePrice).delete(); s.commit()
    assert service.resolve_due(s, now=sig2.resolve_at + dt.timedelta(seconds=200)) == 0  # still inside the backfill window: waits
    assert service.resolve_due(s, now=sig2.resolve_at + dt.timedelta(hours=config.BACKFILL_WINDOW_HOURS + 1)) == 1
    s.refresh(sig2)
    assert sig2.result == "void" and sig2.credit == 0.0
    s.close()


def test_one_bad_row_does_not_stall_resolution(client, session_factory):
    a = Agent(client, "s")
    set_price(session_factory, "BTC", 100.0)
    good = a.post("/signals", {"pair": "BTC", "horizon": "15m", "direction": "long", "confidence": 0.7}).json()["signal_id"]
    s = session_factory()
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    s.add(Signal(id="bad1", agent_id=a.id, pair="ETH", horizon="15m", body_cipher="not-a-token", hash="h",
                 submitted_at=now, resolve_at=now, price_at_submit=1.0))
    s.commit()
    sig = s.query(Signal).filter_by(id=good).first()
    set_price(session_factory, "BTC", 101.0, when=sig.resolve_at)
    set_price(session_factory, "ETH", 1.0, when=now)
    assert service.resolve_due(s, now=sig.resolve_at + dt.timedelta(seconds=5)) == 2
    s.refresh(sig)
    assert sig.result == "right"
    assert s.get(Signal, "bad1").result == "void"
    s.close()


def test_merkle_odd_leaf_not_duplicated_and_frozen_when_anchored(session_factory):
    a, b, c = sha256_hex(b"a"), sha256_hex(b"b"), sha256_hex(b"c")
    assert merkle_root([a, b, c]) != merkle_root([a, b, c, c])
    assert merkle_root([a, b]) == merkle_root([b, a])
    from signal_room.db import DailyLog
    s = session_factory()
    day = dt.date(2026, 1, 1)
    log = service.build_daily_log(s, day)
    log.anchored_tx = "0xabc"; log.merkle_root = "frozen"; s.commit()
    s.add(Signal(agent_id="x", pair="BTC", horizon="1h", body_cipher="c", hash=a, submitted_at=dt.datetime(2026, 1, 1, 12),
                 resolve_at=dt.datetime(2026, 1, 1, 13), price_at_submit=1.0))
    s.commit()
    assert service.build_daily_log(s, day).merkle_root == "frozen"
    view = service.daily_log_view(s, day)
    assert view["anchored_root"] == "frozen" and view["merkle_root"] != "frozen"
    s.close()


def test_query_string_is_signed(client, session_factory):
    a = Agent(client, "s")
    # signing path without the query must fail
    h = a.headers("GET", "/leaderboard")
    assert client.get("/leaderboard?horizon=1h", headers=h).status_code == 401
    assert a.get("/leaderboard", params={"horizon": "1h"}).status_code == 200


def test_solana_base58_signature_accepted(client, session_factory):
    import base58
    from nacl.signing import SigningKey
    key = SigningKey.generate()
    addr = base58.b58encode(bytes(key.verify_key)).decode()
    ch = client.post("/agents/challenge", json={"wallet": addr}).json()
    sig_b58 = base58.b58encode(key.sign(ch["message"].encode()).signature).decode()
    assert client.post("/agents", json={"wallet": addr, "nonce": ch["nonce"], "signature": sig_b58}).status_code == 201


def test_registration_throttle(client, session_factory, monkeypatch):
    from signal_room import api
    monkeypatch.setattr(config, "REGISTRATIONS_PER_HOUR", 2)
    api._reg_times.clear()
    from eth_account import Account
    codes = [client.post("/agents/challenge", json={"wallet": Account.create().address}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_kakalshi_adapter_helpers(tmp_path):
    from agents.kakalshi_adapter import load_bets, load_posted, parse_bet, save_posted, strategy_of
    p = tmp_path / "s.json"
    p.write_text('{"bets": [{"id": 1')
    assert load_bets(str(p)) == []
    p.write_text(json.dumps({"bets": [{"id": 1, "ticker": "KXXRP15M-26OCT28-1", "side": "yes", "price_paid": 72, "strategy": "crypto_first_minute"}]}))
    bets = load_bets(str(p))
    assert parse_bet(bets[0]) == ("XRP", "15m", "long", 0.72) and strategy_of(bets[0]) == "crypto-first-minute"
    assert parse_bet({"ticker": "KXHIGHNY-x", "side": "yes", "price_paid": 50}) is None
    st = tmp_path / "posted.json"
    assert load_posted(str(st)) == set()
    save_posted(str(st), {"1@t"})
    assert load_posted(str(st)) == {"1@t"}
    st.write_text("{broken")
    assert load_posted(str(st)) == set()


def test_momentum_slot_timing():
    from agents.momentum_control import POST_OFFSET_SECONDS, seconds_until_next_slot
    hour = 1_700_000_000 - (1_700_000_000 % 3600)
    assert seconds_until_next_slot(hour + 10) == pytest.approx(3600 - 10 + POST_OFFSET_SECONDS)
    assert seconds_until_next_slot(hour + 3599) == pytest.approx(1 + POST_OFFSET_SECONDS)


def test_mean_reversion_is_momentum_mirrored(monkeypatch):
    from agents import mean_reversion_control as mr, momentum_control as mo
    posted = {}
    class C:
        def post_signal(self, pair, horizon, direction, conf):
            posted.setdefault(pair, []).append(direction); return {"signal_id": "x", "price_at_submit": 1.0}
    monkeypatch.setattr(mo, "last_hour_return", lambda http, pair: 0.01 if pair in ("BTC", "ETH") else -0.01)
    monkeypatch.setattr(mr, "last_hour_return", mo.last_hour_return)
    mo.run_once(C(), None); mr.run_once(C(), None)
    assert posted == {"BTC": ["long", "short"], "ETH": ["long", "short"], "SOL": ["short", "long"], "XRP": ["short", "long"]}

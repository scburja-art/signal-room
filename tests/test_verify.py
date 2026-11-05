"""`verify` recomputes a day's record independently and catches each kind of tampering."""
import datetime as dt

import pytest

from agents import verify
from signal_room import config, service
from signal_room.db import DailyLog, Signal, utcnow
from signal_room.sealing import seal, signal_hash
from tests.api_helpers import Agent
from tests.test_delisting import post_and_resolve


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 10_000)


class ApiClient:
    """The verify tool's view of the room: only what any registered agent can fetch."""
    def __init__(self, agent):
        self.agent = agent

    def log(self, day):
        return self.agent.get(f"/log/{day}").json()

    def read(self, sid):
        return self.agent.get(f"/room/{sid}").json()


def anchored_day(client, factory, n=3):
    author = Agent(client, "author")
    post_and_resolve(client, factory, author, n, right=True, confidence=0.7)
    day = utcnow().date()
    s = factory()
    log = service.build_daily_log(s, day)
    log.anchored_tx = "0x" + "ab" * 32
    s.commit()
    root = log.merkle_root
    s.close()
    chain = lambda tx: {"found": True, "success": True, "payload": verify.expected_payload(day.isoformat(), root),
                        "time": dt.datetime.now(dt.timezone.utc), "sender": "0xabc"}
    return day.isoformat(), chain, ApiClient(Agent(client, "outsider"))


def test_a_clean_day_verifies(client, session_factory):
    day, chain, outsider = anchored_day(client, session_factory)
    r = verify.verify_day(outsider, day, chain=chain, pause=0)
    assert r["ok"] and r["checked"] == 3 and r["matched"] == 3
    assert "VERIFIED" in verify.report(r) and "✘" not in verify.report(r)


def test_a_changed_call_is_caught(client, session_factory):
    day, chain, outsider = anchored_day(client, session_factory)
    s = session_factory()
    sig = s.query(Signal).first()
    sig.price_at_submit += 1.0          # history rewritten after the fact
    s.commit()
    s.close()
    r = verify.verify_day(outsider, day, chain=chain, pause=0)
    assert not r["ok"] and len(r["mismatched"]) == 1
    assert "do NOT match their published fingerprint" in verify.report(r)


def test_an_added_call_is_caught(client, session_factory):
    day, chain, outsider = anchored_day(client, session_factory)
    s = session_factory()
    extra = s.query(Signal).first()
    s.add(Signal(agent_id=extra.agent_id, pair="ETH", horizon="1h", body_cipher=extra.body_cipher, hash="cd" * 32,
                 submitted_at=extra.submitted_at, resolve_at=extra.resolve_at, price_at_submit=1.0))
    s.commit()
    s.close()
    r = verify.verify_day(outsider, day, chain=chain, pause=0)
    assert not r["root_matches_anchor"] and not r["chain_matches"] and not r["ok"]


def test_a_rewritten_anchor_record_is_caught(client, session_factory):
    day, chain, outsider = anchored_day(client, session_factory)
    s = session_factory()
    s.get(DailyLog, day).merkle_root = "00" * 32     # someone edits the stored root to hide changes
    s.commit()
    s.close()
    r = verify.verify_day(outsider, day, chain=chain, pause=0)
    assert not r["root_matches_anchor"] and r["chain_matches"]   # the chain still holds the real root
    assert not r["ok"]


def test_chain_payload_must_match(client, session_factory):
    day, chain, outsider = anchored_day(client, session_factory)
    wrong = lambda tx: {**chain(tx), "payload": verify.expected_payload(day, "11" * 32)}
    r = verify.verify_day(outsider, day, chain=wrong, pause=0)
    assert not r["chain_matches"] and "does NOT carry this fingerprint" in verify.report(r)


def test_independent_math_matches_the_room():
    from signal_room.sealing import merkle_root
    hs = [f"{i:064x}" for i in range(1, 8)]
    for k in range(0, 8):
        assert verify.merkle_root(hs[:k]) == merkle_root(hs[:k])
    args = ("agent", "BTC", "1h", "long", 0.55, "2026-11-03T14:00:01.123456", 77400.5)
    assert verify.call_hash(*args, "f" * 32) == signal_hash(*args, "f" * 32)


def test_calls_from_before_salts_still_verify(client, session_factory):
    """Calls posted before 2026-10-28 were hashed without a salt; the tool recomputes them in that format."""
    author = Agent(client, "old-author")
    when = utcnow().replace(microsecond=123456)
    legacy = verify.call_hash(author.id, "BTC", "1h", "short", 0.6, when.isoformat(), 70000.0, None)
    s = session_factory()
    s.add(Signal(agent_id=author.id, pair="BTC", horizon="1h", body_cipher=seal(author.id, {"direction": "short", "confidence": 0.6}),
                 hash=legacy, submitted_at=when, resolve_at=when + dt.timedelta(hours=1), price_at_submit=70000.0,
                 resolved_at=when + dt.timedelta(hours=1), result="right", direction="short", confidence=0.6))
    s.commit()
    log = service.build_daily_log(s, when.date())
    log.anchored_tx = "0x" + "ef" * 32
    s.commit()
    root = log.merkle_root
    s.close()
    chain = lambda tx: {"found": True, "success": True, "payload": verify.expected_payload(when.date().isoformat(), root),
                        "time": dt.datetime.now(dt.timezone.utc)}
    r = verify.verify_day(ApiClient(Agent(client, "outsider")), when.date().isoformat(), chain=chain, pause=0)
    assert r["ok"] and r["matched"] == 1


def test_open_calls_never_reveal_their_salt(client, session_factory):
    from tests.conftest import set_price
    author, reader = Agent(client, "a"), Agent(client, "r")
    set_price(session_factory, "SOL", 95.0)
    sid = author.post("/signals", {"pair": "SOL", "horizon": "1h", "direction": "long", "confidence": 0.6}).json()["signal_id"]
    assert reader.get(f"/room/{sid}").json()["salt"] is None


def test_chain_lookup_reads_a_real_shaped_reply(monkeypatch):
    payload = verify.expected_payload("2026-11-03", "d66ff0f897074ebff05123091ae675dae068a0713b8ae84341f07c9d21603672")
    replies = {
        "eth_getTransactionByHash": {"input": "0x" + payload.encode().hex(), "blockNumber": "0x33e3a2e",
                                     "from": "0xea11b5e115f0a6ce69fc3da217e66d1199411a32"},
        "eth_getTransactionReceipt": {"status": "0x1"},
        "eth_getBlockByNumber": {"timestamp": hex(1793803823)},
    }

    class R:
        def __init__(self, body): self.body = body
        def raise_for_status(self): pass
        def json(self): return {"jsonrpc": "2.0", "id": 1, "result": self.body}

    monkeypatch.setattr(verify.httpx, "post", lambda url, json, timeout: R(replies[json["method"]]))
    c = verify.chain_lookup("0x3520d47162353a54d44c5306de3fbe40934a42000c6ba8d785aa2809ee1a2c35")
    assert c["found"] and c["success"] and c["payload"] == payload
    assert c["time"] == dt.datetime(2026, 11, 4, 14, 50, 23, tzinfo=dt.timezone.utc)

"""End-to-end: register two agents, post, resolve with fake prices, read with credits, verify the log."""
import datetime as dt
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from signal_room import service
from signal_room.db import utcnow
from signal_room.sealing import merkle_root
from tests.conftest import set_price


class Agent:
    def __init__(self, client, name):
        self.client = client
        self.acct = Account.create()
        ch = client.post("/agents/challenge", json={"wallet": self.acct.address}).json()
        sig = Account.sign_message(encode_defunct(text=ch["message"]), private_key=self.acct.key).signature.hex()
        r = client.post("/agents", json={"wallet": self.acct.address, "nonce": ch["nonce"], "signature": sig, "name": name})
        assert r.status_code == 201, r.text
        self.id = r.json()["agent_id"]
        self.key = r.json()["api_key"]

    def headers(self, method, path, body=b"", ts=None, nonce=None, key=None, query=""):
        ts = ts or str(int(time.time()))
        nonce = nonce or secrets.token_hex(16)
        target = f"{path}?{query}" if query else path
        msg = f"{method}\n{target}\n{ts}\n{nonce}\n{hashlib.sha256(body).hexdigest()}"
        sig = Account.sign_message(encode_defunct(text=msg), private_key=self.acct.key).signature.hex()
        return {"X-Agent-Key": key or self.key, "X-Timestamp": ts, "X-Nonce": nonce, "X-Signature": sig,
                "Content-Type": "application/json"}

    def post(self, path, payload, **kw):
        body = json.dumps(payload, separators=(",", ":")).encode()
        return self.client.post(path, content=body, headers=self.headers("POST", path, body, **kw))

    def get(self, path, params=None, **kw):
        query = urlencode(params) if params else ""
        url = f"{path}?{query}" if query else path
        return self.client.get(url, headers=self.headers("GET", path, query=query, **kw))


def test_registration_requires_valid_signature(client):
    acct = Account.create()
    ch = client.post("/agents/challenge", json={"wallet": acct.address}).json()
    other = Account.create()
    bad = Account.sign_message(encode_defunct(text=ch["message"]), private_key=other.key).signature.hex()
    r = client.post("/agents", json={"wallet": acct.address, "nonce": ch["nonce"], "signature": bad})
    assert r.status_code == 401


def test_signed_request_rules(client, session_factory):
    a = Agent(client, "a")
    set_price(session_factory, "BTC", 100.0)
    path, payload = "/signals", {"pair": "BTC", "horizon": "1h", "direction": "long", "confidence": 0.6}
    # good
    assert a.post(path, payload).status_code == 201
    # replayed nonce
    body = json.dumps(payload, separators=(",", ":")).encode()
    h = a.headers("POST", "/signals", body)
    payload2 = {**payload, "horizon": "4h"}
    body2 = json.dumps(payload2, separators=(",", ":")).encode()
    h2 = a.headers("POST", "/signals", body2, nonce=h["X-Nonce"])
    assert client.post(path, content=body2, headers=h2).status_code == 201
    assert client.post(path, content=body2, headers=h2).status_code == 401  # same nonce twice
    # stale timestamp
    old = str(int(time.time()) - 120)
    payload3 = {**payload, "horizon": "24h"}
    body3 = json.dumps(payload3, separators=(",", ":")).encode()
    assert client.post(path, content=body3, headers=a.headers("POST", path, body3, ts=old)).status_code == 401
    # wrong key for this wallet
    b = Agent(client, "b")
    assert client.post(path, content=body3, headers=a.headers("POST", path, body3, key=b.key)).status_code == 401
    # unsigned
    assert client.post(path, json=payload3).status_code == 422


def test_full_flow(client, session_factory):
    seller = Agent(client, "seller")
    reader = Agent(client, "reader")
    set_price(session_factory, "ETH", 2000.0)

    # one open signal per pair per horizon
    r = seller.post("/signals", {"pair": "ETH", "horizon": "15m", "direction": "long", "confidence": 0.8})
    assert r.status_code == 201, r.text
    sid = r.json()["signal_id"]
    assert seller.post("/signals", {"pair": "ETH", "horizon": "15m", "direction": "short", "confidence": 0.8}).status_code == 409
    # validation
    assert seller.post("/signals", {"pair": "DOGE", "horizon": "15m", "direction": "long", "confidence": 0.8}).status_code == 422
    assert seller.post("/signals", {"pair": "BTC", "horizon": "15m", "direction": "long", "confidence": 0.3}).status_code == 422

    # listing hides direction and confidence
    listing = reader.get("/room").json()["signals"]
    assert len(listing) == 1 and listing[0]["signal_id"] == sid
    assert "direction" not in listing[0] and "confidence" not in listing[0]

    # reading costs a credit; starter balance is 10
    assert reader.get("/me").json()["credits"] == 10.0
    body = reader.get(f"/room/{sid}").json()
    assert body["direction"] == "long" and body["confidence"] == 0.8
    assert reader.get("/me").json()["credits"] == 9.0
    reader.get(f"/room/{sid}")  # second read of the same signal is free
    assert reader.get("/me").json()["credits"] == 9.0
    # own signal is free
    seller.get(f"/room/{sid}")
    assert seller.get("/me").json()["credits"] == 10.0

    # resolve: price went up 1% at the horizon → right, BS 0.04, credit 0.84
    from signal_room.db import Signal
    s = session_factory()
    sig = s.query(Signal).filter_by(id=sid).first()
    set_price(session_factory, "ETH", 2020.0, when=sig.resolve_at)
    resolved = service.resolve_due(s, now=sig.resolve_at + dt.timedelta(seconds=5))
    assert resolved == 1
    s.close()

    me = seller.get("/me").json()
    assert me["credits"] == pytest.approx(10.84)
    done = me["signals"][0]
    assert done["result"] == "right" and done["brier"] == pytest.approx(0.04)
    assert done["net_return"] == pytest.approx(0.009)
    assert me["records"]["15m"]["n"] == 1 and me["records"]["15m"]["ranked"] is False

    # public record is visible; leaderboard is empty under 30 resolved
    pub = reader.get(f"/agents/{seller.id}").json()
    assert pub["n"] == 1 and pub["bss"] == pytest.approx(0.84)
    assert reader.get("/leaderboard").json()["agents"] == []

    # daily log carries the hash and a matching merkle root
    log = reader.get(f"/log/{utcnow().date().isoformat()}").json()
    assert [x["hash"] for x in log["signals"]] == [done["hash"]]
    assert log["merkle_root"] == merkle_root([done["hash"]])


def test_insufficient_credits(client, session_factory):
    seller = Agent(client, "s")
    broke = Agent(client, "broke")
    for pair, price in (("SOL", 50.0), ("XRP", 0.5), ("BTC", 100.0)):
        set_price(session_factory, pair, price)
    ids = []
    for pair, horizons in (("SOL", ("15m", "1h", "4h", "24h")), ("XRP", ("15m", "1h", "4h", "24h")), ("BTC", ("15m", "1h", "4h"))):
        for h in horizons:
            r = seller.post("/signals", {"pair": pair, "horizon": h, "direction": "short", "confidence": 0.7})
            assert r.status_code == 201, r.text
            ids.append(r.json()["signal_id"])
    assert len(ids) == 11
    for sid in ids[:10]:
        assert broke.get(f"/room/{sid}").status_code == 200
    assert broke.get("/me").json()["credits"] == 0.0
    r = broke.get(f"/room/{ids[10]}")
    assert r.status_code == 402


def test_leaderboard_ranks_after_30(client, session_factory):
    a = Agent(client, "steady")
    from signal_room.db import Signal
    from signal_room.db import ReferencePrice
    for i in range(31):
        s = session_factory()
        s.query(ReferencePrice).filter_by(pair="BTC").delete(); s.commit(); s.close()  # fresh tape each round
        set_price(session_factory, "BTC", 100.0)
        r = a.post("/signals", {"pair": "BTC", "horizon": "15m", "direction": "long", "confidence": 0.7})
        assert r.status_code == 201, r.text
        s = session_factory()
        sig = s.query(Signal).filter_by(id=r.json()["signal_id"]).first()
        set_price(session_factory, "BTC", 101.0 if i % 4 else 99.0, when=sig.resolve_at)  # 3 right : 1 wrong
        service.resolve_due(s, now=sig.resolve_at + dt.timedelta(seconds=1))
        s.close()
    lb = a.get("/leaderboard", params={"horizon": "15m"}).json()["agents"]
    assert len(lb) == 1 and lb[0]["rank"] == 1 and lb[0]["n"] == 31 and lb[0]["score"] > 0

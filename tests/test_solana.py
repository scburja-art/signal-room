"""A Solana (Ed25519) wallet can register and sign requests, and a wrong key is rejected."""
import hashlib
import json
import secrets
import time

import base58
from nacl.signing import SigningKey

from signal_room import auth
from tests.conftest import set_price


def sol_wallet():
    k = SigningKey.generate()
    return k, base58.b58encode(bytes(k.verify_key)).decode()


def headers(key, api_key, method, path, body=b""):
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    msg = f"{method}\n{path}\n{ts}\n{nonce}\n{hashlib.sha256(body).hexdigest()}"
    sig = key.sign(msg.encode()).signature.hex()
    return {"X-Agent-Key": api_key, "X-Timestamp": ts, "X-Nonce": nonce, "X-Signature": sig, "Content-Type": "application/json"}


def test_wallet_type_detection():
    assert auth.wallet_type("0x" + "ab" * 20) == "evm"
    _, addr = sol_wallet()
    assert auth.wallet_type(addr) == "solana"
    assert auth.canonical_wallet(addr) == addr            # case preserved
    assert auth.canonical_wallet("0xABCDEF" + "0" * 34) == "0xabcdef" + "0" * 34


def test_solana_register_and_post(client, session_factory):
    key, addr = sol_wallet()
    ch = client.post("/agents/challenge", json={"wallet": addr}).json()
    sig = key.sign(ch["message"].encode()).signature.hex()
    r = client.post("/agents", json={"wallet": addr, "nonce": ch["nonce"], "signature": sig, "name": "sol"})
    assert r.status_code == 201, r.text
    assert r.json()["wallet_type"] == "solana"
    api_key = r.json()["api_key"]

    set_price(session_factory, "SOL", 100.0)
    payload = {"pair": "SOL", "horizon": "1h", "direction": "long", "confidence": 0.7}
    body = json.dumps(payload, separators=(",", ":")).encode()
    assert client.post("/signals", content=body, headers=headers(key, api_key, "POST", "/signals", body)).status_code == 201

    # base58-encoded signature is accepted too
    me_h = headers(key, api_key, "GET", "/me")
    assert client.get("/me", headers=me_h).status_code == 200

    # a different key with the right api key is rejected
    other, _ = sol_wallet()
    assert client.get("/me", headers=headers(other, api_key, "GET", "/me")).status_code == 401


def test_solana_registration_wrong_key_rejected(client):
    key, addr = sol_wallet()
    other, _ = sol_wallet()
    ch = client.post("/agents/challenge", json={"wallet": addr}).json()
    bad = other.sign(ch["message"].encode()).signature.hex()
    assert client.post("/agents", json={"wallet": addr, "nonce": ch["nonce"], "signature": bad}).status_code == 401


def test_client_library_both_chains(client, session_factory, tmp_path, monkeypatch):
    """The agents.client helper registers and signs with either wallet type against the test app."""
    import httpx
    from agents import client as clientmod
    RealClient = httpx.Client

    class SyncASGI(httpx.BaseTransport):
        """Drive the FastAPI app synchronously so the real client code runs unchanged."""
        def handle_request(self, request):
            r = client.request(request.method, str(request.url), content=request.content, headers=dict(request.headers))
            return httpx.Response(r.status_code, headers=r.headers, content=r.content)

    monkeypatch.setattr(clientmod.httpx, "Client", lambda *a, **k: RealClient(transport=SyncASGI(), base_url="http://t", timeout=15.0))
    set_price(session_factory, "XRP", 1.0)
    for chain in ("evm", "solana"):
        c = clientmod.SignalRoomClient.load_or_register("http://t", str(tmp_path / f"{chain}.json"), name=chain, chain=chain)
        assert c.post_signal("XRP", "15m", "short", 0.6)["signal_id"]
        assert c.me()["name"] == chain
        c2 = clientmod.SignalRoomClient.load_or_register("http://t", str(tmp_path / f"{chain}.json"))  # reload from state
        assert c2.agent_id == c.agent_id and c2.wallet.chain == chain

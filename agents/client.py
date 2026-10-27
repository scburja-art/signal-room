"""Minimal Signal Room client. Any agent can integrate with this in ten lines.

    from agents.client import SignalRoomClient
    c = SignalRoomClient.load_or_register("http://127.0.0.1:8000", "agents/state/my_agent.json", name="my-agent")
    c.post_signal("BTC", "1h", "long", 0.62)
    print(c.me()["credits"])

Pass chain="solana" to load_or_register for an Ed25519 (Solana) wallet; default is an EVM wallet.
The state file holds the wallet private key and the API key. Keep it out of git (agents/state/ is ignored).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time

import base58
import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from nacl.signing import SigningKey


class Wallet:
    """One of two signing wallets. address + sign(message) is all the client needs."""

    def __init__(self, chain: str, private_key: str):
        self.chain = chain
        if chain == "evm":
            self._acct = Account.from_key(private_key)
            self.address = self._acct.address
        elif chain == "solana":
            self._key = SigningKey(bytes.fromhex(private_key))
            self.address = base58.b58encode(bytes(self._key.verify_key)).decode()
        else:
            raise ValueError(f"unknown chain {chain}")
        self.private_key = private_key

    @classmethod
    def create(cls, chain: str) -> "Wallet":
        if chain == "evm":
            return cls("evm", Account.create().key.hex())
        if chain == "solana":
            return cls("solana", SigningKey.generate().encode().hex())
        raise ValueError(f"unknown chain {chain}")

    def sign(self, message: str) -> str:
        if self.chain == "evm":
            return Account.sign_message(encode_defunct(text=message), private_key=self._acct.key).signature.hex()
        return self._key.sign(message.encode()).signature.hex()


class SignalRoomClient:
    def __init__(self, base_url: str, private_key: str, api_key: str, agent_id: str, chain: str = "evm"):
        self.base_url = base_url.rstrip("/")
        self.wallet = Wallet(chain, private_key)
        self.api_key = api_key
        self.agent_id = agent_id
        self.http = httpx.Client(timeout=15.0)

    # ----- registration -----

    @classmethod
    def load_or_register(cls, base_url: str, state_path: str, name: str = "", chain: str = "evm") -> "SignalRoomClient":
        if os.path.exists(state_path):
            with open(state_path) as f:
                s = json.load(f)
            return cls(base_url, s["private_key"], s["api_key"], s["agent_id"], s.get("chain", "evm"))
        w = Wallet.create(chain)
        http = httpx.Client(timeout=15.0)
        ch = http.post(f"{base_url}/agents/challenge", json={"wallet": w.address}).json()
        r = http.post(f"{base_url}/agents", json={"wallet": w.address, "nonce": ch["nonce"], "signature": w.sign(ch["message"]), "name": name})
        r.raise_for_status()
        reg = r.json()
        os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
        with open(state_path, "w") as f:
            json.dump({"chain": chain, "private_key": w.private_key, "api_key": reg["api_key"], "agent_id": reg["agent_id"],
                       "wallet": w.address}, f)
        os.chmod(state_path, 0o600)
        return cls(base_url, w.private_key, reg["api_key"], reg["agent_id"], chain)

    # ----- signed requests -----

    def _headers(self, method: str, path: str, body: bytes) -> dict:
        ts = str(int(time.time()))
        nonce = secrets.token_hex(16)
        msg = f"{method.upper()}\n{path}\n{ts}\n{nonce}\n{hashlib.sha256(body).hexdigest()}"
        return {"X-Agent-Key": self.api_key, "X-Timestamp": ts, "X-Nonce": nonce, "X-Signature": self.wallet.sign(msg),
                "Content-Type": "application/json"}

    def _call(self, method: str, path: str, json_body: dict | None = None, params: dict | None = None):
        body = json.dumps(json_body, separators=(",", ":")).encode() if json_body is not None else b""
        r = self.http.request(method, self.base_url + path, content=body, params=params,
                              headers=self._headers(method, path, body))
        if r.status_code >= 400:
            raise SignalRoomError(r.status_code, r.text)
        return r.json()

    # ----- API -----

    def post_signal(self, pair: str, horizon: str, direction: str, confidence: float) -> dict:
        return self._call("POST", "/signals", {"pair": pair, "horizon": horizon, "direction": direction, "confidence": confidence})

    def room(self, pair: str | None = None, horizon: str | None = None) -> list[dict]:
        return self._call("GET", "/room", params={k: v for k, v in {"pair": pair, "horizon": horizon}.items() if v})["signals"]

    def read(self, signal_id: str) -> dict:
        return self._call("GET", f"/room/{signal_id}")

    def me(self) -> dict:
        return self._call("GET", "/me")

    def agent(self, agent_id: str, horizon: str | None = None, window: str = "90d") -> dict:
        return self._call("GET", f"/agents/{agent_id}", params={k: v for k, v in {"horizon": horizon, "window": window}.items() if v})

    def leaderboard(self, horizon: str | None = None, pair: str | None = None, window: str = "90d") -> dict:
        return self._call("GET", "/leaderboard", params={k: v for k, v in {"horizon": horizon, "pair": pair, "window": window}.items() if v})

    def log(self, day: str) -> dict:
        return self._call("GET", f"/log/{day}")


class SignalRoomError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"{status}: {text}")
        self.status = status
        self.text = text

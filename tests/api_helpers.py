"""Signed test agent shared by the API tests."""
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

from eth_account import Account
from eth_account.messages import encode_defunct


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

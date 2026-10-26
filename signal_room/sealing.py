"""Hashing and encryption of signal bodies."""
from __future__ import annotations

import base64
import hashlib
import json

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import config


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def signal_hash(agent_id: str, pair: str, horizon: str, direction: str, confidence: float,
                submitted_at_iso: str, price_at_submit: float) -> str:
    """Hash over everything that defines the prediction, including the server stamp."""
    return sha256_hex(canonical({
        "agent": agent_id, "pair": pair, "horizon": horizon, "direction": direction,
        "confidence": round(confidence, 4), "submitted_at": submitted_at_iso,
        "price_at_submit": price_at_submit,
    }))


def _agent_fernet(agent_id: str) -> Fernet:
    if not config.MASTER_KEY:
        raise RuntimeError("SIGNAL_ROOM_MASTER_KEY is not set")
    master = base64.urlsafe_b64decode(config.MASTER_KEY)
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                   info=f"signal-room:agent:{agent_id}".encode()).derive(master)
    return Fernet(base64.urlsafe_b64encode(derived))


def seal(agent_id: str, body: dict) -> str:
    return _agent_fernet(agent_id).encrypt(canonical(body)).decode()


def unseal(agent_id: str, cipher: str) -> dict:
    return json.loads(_agent_fernet(agent_id).decrypt(cipher.encode()))


def merkle_root(hashes_hex: list[str]) -> str:
    """Merkle root over a list of hex hashes, sorted for determinism. Empty list → hash of empty."""
    if not hashes_hex:
        return sha256_hex(b"")
    layer = [bytes.fromhex(h) for h in sorted(hashes_hex)]
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [hashlib.sha256(layer[i] + layer[i + 1]).digest() for i in range(0, len(layer), 2)]
    return layer[0].hex()

"""Hashing and encryption of signal bodies."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import config


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def new_salt() -> str:
    return secrets.token_hex(16)


def signal_hash(agent_id: str, pair: str, horizon: str, direction: str, confidence: float,
                submitted_at_iso: str, price_at_submit: float, salt: str) -> str:
    """Commitment to the prediction plus the server stamp.

    The salt is random per signal and stays inside the encrypted body until the signal resolves.
    Without it, everything else in the hash is public or guessable (the listing shows pair,
    horizon, author and time; the reference price is public), so a reader could brute-force
    direction and confidence from the hash alone. With a 128-bit salt that is infeasible.
    """
    return sha256_hex(canonical({
        "agent": agent_id, "pair": pair, "horizon": horizon, "direction": direction,
        "confidence": round(confidence, 4), "submitted_at": submitted_at_iso,
        "price_at_submit": price_at_submit, "salt": salt,
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
    """Merkle root over a list of hex hashes, sorted for determinism (RFC 6962 shape).

    Leaves are hashed with a 0x00 prefix and inner nodes with 0x01, and an odd node is carried
    up unchanged rather than duplicated, so [a, b, c] and [a, b, c, c] cannot share a root.
    Empty list → hash of empty.
    """
    if not hashes_hex:
        return sha256_hex(b"")
    layer = [hashlib.sha256(b"\x00" + bytes.fromhex(h)).digest() for h in sorted(hashes_hex)]
    while len(layer) > 1:
        nxt = [hashlib.sha256(b"\x01" + layer[i] + layer[i + 1]).digest() for i in range(0, len(layer) - 1, 2)]
        if len(layer) % 2:
            nxt.append(layer[-1])
        layer = nxt
    return layer[0].hex()

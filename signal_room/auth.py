"""Wallet-based identity and signed requests.

Two wallet types:
  EVM     0x + 40 hex. EIP-191 personal_sign (secp256k1). Address is lowercased.
  Solana  base58 32-byte public key. Ed25519 detached signature over the UTF-8 message,
          sent as hex or base58. Address is case-sensitive and stored as given.

Registration: the agent asks for a challenge nonce, signs
"Signal Room registration\nwallet: <addr>\nnonce: <nonce>" with its wallet key, and
receives an API key (shown once; only its hash is stored).

Every later request carries:
  X-Agent-Key   the API key
  X-Timestamp   unix seconds
  X-Nonce       random hex, single use
  X-Signature   signature over "METHOD\nPATH?QUERY\nTIMESTAMP\nNONCE\nSHA256(BODY)"
                (PATH?QUERY is the path plus the raw query string when there is one)
The signature must verify against the agent's wallet.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import secrets
import threading

import base58
from eth_account import Account
from eth_account.messages import encode_defunct
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from fastapi import Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from . import config
from .db import Agent, AuditEvent, Nonce, RegistrationChallenge, RequestCount, get_session, utcnow
from .sealing import sha256_hex


EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
SOLANA_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
WALLET_PATTERN = r"^(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})$"


def wallet_type(wallet: str) -> str:
    if EVM_RE.match(wallet):
        return "evm"
    if SOLANA_RE.match(wallet):
        try:
            if len(base58.b58decode(wallet)) == 32:
                return "solana"
        except ValueError:
            pass
    raise HTTPException(422, "wallet must be an EVM address (0x...) or a Solana public key (base58)")


def canonical_wallet(wallet: str) -> str:
    """EVM addresses lowercase; Solana keys are case-sensitive and kept as given."""
    return wallet.lower() if wallet_type(wallet) == "evm" else wallet


def registration_message(wallet: str, nonce: str) -> str:
    return f"Signal Room registration\nwallet: {canonical_wallet(wallet)}\nnonce: {nonce}"


def request_message(method: str, path: str, timestamp: str, nonce: str, body: bytes, query: str = "") -> str:
    target = f"{path}?{query}" if query else path
    return f"{method.upper()}\n{target}\n{timestamp}\n{nonce}\n{sha256_hex(body)}"


def _sig_bytes(signature: str) -> bytes:
    sig = signature[2:] if signature.startswith("0x") else signature
    try:
        b = bytes.fromhex(sig)
        if len(b) == 64:
            return b
    except ValueError:
        pass
    try:
        b = base58.b58decode(sig)
        if len(b) == 64:
            return b
    except ValueError:
        pass
    raise HTTPException(401, "bad signature")


def verify(message: str, signature: str, wallet: str) -> bool:
    """True when `signature` over `message` was made by `wallet`'s key."""
    kind = wallet_type(wallet)
    if kind == "evm":
        try:
            return Account.recover_message(encode_defunct(text=message), signature=signature).lower() == wallet.lower()
        except Exception:
            return False
    try:
        VerifyKey(base58.b58decode(wallet)).verify(message.encode(), _sig_bytes(signature))
        return True
    except (BadSignatureError, HTTPException, ValueError):
        return False


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_api_key() -> str:
    return "sr_" + secrets.token_hex(24)


def new_challenge(db: Session, wallet: str) -> str:
    wallet = canonical_wallet(wallet)
    nonce = secrets.token_hex(16)
    db.add(RegistrationChallenge(nonce=nonce, wallet=wallet))
    db.commit()
    return nonce


def consume_challenge(db: Session, wallet: str, nonce: str) -> None:
    ch = db.get(RegistrationChallenge, nonce)
    if not ch or ch.wallet != canonical_wallet(wallet):
        raise HTTPException(400, "unknown challenge")
    if (utcnow() - ch.created_at).total_seconds() > 600:
        db.delete(ch); db.commit()
        raise HTTPException(400, "challenge expired")
    db.delete(ch)
    db.commit()


_rate_lock = threading.Lock()


def _rate_limit(db: Session, agent_id: str) -> None:
    minute = utcnow().strftime("%Y%m%d%H%M")
    with _rate_lock:
        row = db.get(RequestCount, (agent_id, minute))
        if row is None:
            row = RequestCount(agent_id=agent_id, minute=minute, count=0)
            db.add(row)
        row.count += 1
        db.commit()
        count = row.count
    if count > config.RATE_LIMIT_PER_MINUTE:
        raise HTTPException(429, "rate limit")


async def signed_agent(
    request: Request,
    db: Session = Depends(get_session),
    x_agent_key: str = Header(...),
    x_timestamp: str = Header(...),
    x_nonce: str = Header(...),
    x_signature: str = Header(...),
) -> Agent:
    """Reads the body on the event loop, then does the database and signature work in a worker
    thread, so one slow verification cannot stall every other request."""
    body = await request.body()
    return await run_in_threadpool(
        authenticate, db, request.method, request.url.path, request.url.query, body,
        x_agent_key, x_timestamp, x_nonce, x_signature,
    )


def authenticate(db: Session, method: str, path: str, query: str, body: bytes,
                 api_key: str, timestamp: str, nonce: str, signature: str) -> Agent:
    agent = db.query(Agent).filter_by(api_key_hash=hash_api_key(api_key)).first()
    if not agent:
        raise HTTPException(401, "unknown key")
    _rate_limit(db, agent.id)  # before any signature work, so a stolen key alone cannot burn CPU
    if agent.status == "retired":
        raise HTTPException(403, "wallet retired")
    try:
        ts = int(timestamp)
    except ValueError:
        raise HTTPException(401, "bad timestamp")
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    if abs(now - ts) > config.SIGNATURE_WINDOW_SECONDS:
        raise HTTPException(401, "timestamp outside window")
    if not (8 <= len(nonce) <= 64):
        raise HTTPException(401, "bad nonce")
    if db.get(Nonce, (agent.id, nonce)):
        db.add(AuditEvent(agent_id=agent.id, event="replay_rejected", detail=nonce))
        db.commit()
        raise HTTPException(401, "nonce reused")
    msg = request_message(method, path, timestamp, nonce, body, query)
    if not verify(msg, signature, agent.wallet):
        raise HTTPException(401, "signature does not match wallet")
    db.add(Nonce(agent_id=agent.id, nonce=nonce))
    db.commit()
    return agent


def purge_old(db: Session) -> None:
    """Drop nonces and challenges older than the window; called by the scheduler."""
    cutoff = utcnow() - dt.timedelta(seconds=config.SIGNATURE_WINDOW_SECONDS * 3)
    db.query(Nonce).filter(Nonce.created_at < cutoff).delete()
    db.query(RegistrationChallenge).filter(RegistrationChallenge.created_at < utcnow() - dt.timedelta(minutes=10)).delete()
    old_minute = (utcnow() - dt.timedelta(minutes=5)).strftime("%Y%m%d%H%M")
    db.query(RequestCount).filter(RequestCount.minute < old_minute).delete()
    db.commit()

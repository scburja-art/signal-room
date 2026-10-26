"""Wallet-based identity and signed requests.

Registration: the agent asks for a challenge nonce, signs
"Signal Room registration\nwallet: <addr>\nnonce: <nonce>" with its wallet key, and
receives an API key (shown once; only its hash is stored).

Every later request carries:
  X-Agent-Key   the API key
  X-Timestamp   unix seconds
  X-Nonce       random hex, single use
  X-Signature   EIP-191 signature over "METHOD\nPATH\nTIMESTAMP\nNONCE\nSHA256(BODY)"
The signature must recover to the agent's wallet.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from . import config
from .db import Agent, AuditEvent, Nonce, RegistrationChallenge, RequestCount, get_session, utcnow
from .sealing import sha256_hex


def registration_message(wallet: str, nonce: str) -> str:
    return f"Signal Room registration\nwallet: {wallet.lower()}\nnonce: {nonce}"


def request_message(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    return f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{sha256_hex(body)}"


def recover(message: str, signature: str) -> str:
    try:
        return Account.recover_message(encode_defunct(text=message), signature=signature).lower()
    except Exception:
        raise HTTPException(401, "bad signature")


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_api_key() -> str:
    return "sr_" + secrets.token_hex(24)


def new_challenge(db: Session, wallet: str) -> str:
    wallet = wallet.lower()
    nonce = secrets.token_hex(16)
    db.add(RegistrationChallenge(nonce=nonce, wallet=wallet))
    db.commit()
    return nonce


def consume_challenge(db: Session, wallet: str, nonce: str) -> None:
    ch = db.get(RegistrationChallenge, nonce)
    if not ch or ch.wallet != wallet.lower():
        raise HTTPException(400, "unknown challenge")
    if (utcnow() - ch.created_at).total_seconds() > 600:
        db.delete(ch); db.commit()
        raise HTTPException(400, "challenge expired")
    db.delete(ch)
    db.commit()


def _rate_limit(db: Session, agent_id: str) -> None:
    minute = utcnow().strftime("%Y%m%d%H%M")
    row = db.get(RequestCount, (agent_id, minute))
    if row is None:
        row = RequestCount(agent_id=agent_id, minute=minute, count=0)
        db.add(row)
    row.count += 1
    db.commit()
    if row.count > config.RATE_LIMIT_PER_MINUTE:
        raise HTTPException(429, "rate limit")


async def signed_agent(
    request: Request,
    db: Session = Depends(get_session),
    x_agent_key: str = Header(...),
    x_timestamp: str = Header(...),
    x_nonce: str = Header(...),
    x_signature: str = Header(...),
) -> Agent:
    agent = db.query(Agent).filter_by(api_key_hash=hash_api_key(x_agent_key)).first()
    if not agent:
        raise HTTPException(401, "unknown key")
    if agent.status == "retired":
        raise HTTPException(403, "wallet retired")
    try:
        ts = int(x_timestamp)
    except ValueError:
        raise HTTPException(401, "bad timestamp")
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    if abs(now - ts) > config.SIGNATURE_WINDOW_SECONDS:
        raise HTTPException(401, "timestamp outside window")
    if not (8 <= len(x_nonce) <= 64):
        raise HTTPException(401, "bad nonce")
    if db.get(Nonce, (agent.id, x_nonce)):
        db.add(AuditEvent(agent_id=agent.id, event="replay_rejected", detail=x_nonce))
        db.commit()
        raise HTTPException(401, "nonce reused")
    body = await request.body()
    msg = request_message(request.method, request.url.path, x_timestamp, x_nonce, body)
    if recover(msg, x_signature) != agent.wallet:
        raise HTTPException(401, "signature does not match wallet")
    db.add(Nonce(agent_id=agent.id, nonce=x_nonce))
    db.commit()
    _rate_limit(db, agent.id)
    return agent


def purge_old(db: Session) -> None:
    """Drop nonces and challenges older than the window; called by the scheduler."""
    cutoff = utcnow() - dt.timedelta(seconds=config.SIGNATURE_WINDOW_SECONDS * 3)
    db.query(Nonce).filter(Nonce.created_at < cutoff).delete()
    db.query(RegistrationChallenge).filter(RegistrationChallenge.created_at < utcnow() - dt.timedelta(minutes=10)).delete()
    old_minute = (utcnow() - dt.timedelta(minutes=5)).strftime("%Y%m%d%H%M")
    db.query(RequestCount).filter(RequestCount.minute < old_minute).delete()
    db.commit()

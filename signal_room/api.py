"""The agent API. Every endpoint except registration requires a signed request."""
from __future__ import annotations

import datetime as dt
import threading
import time
from collections import deque

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import auth, config, scoring, service
from .db import Agent, AuditEvent, get_session

app = FastAPI(title="Signal Room", version="0.1", docs_url="/docs", redoc_url=None)

_reg_lock = threading.Lock()
_reg_times: deque = deque()


def _registration_throttle() -> None:
    """Global cap on new wallets per hour. Starter credits are free reads, so unlimited
    registration would be unlimited reads. Membership replaces this in Week 3."""
    now = time.monotonic()
    with _reg_lock:
        while _reg_times and now - _reg_times[0] > 3600:
            _reg_times.popleft()
        if len(_reg_times) >= config.REGISTRATIONS_PER_HOUR:
            raise HTTPException(429, "registration limit reached; try again later")
        _reg_times.append(now)


class ChallengeIn(BaseModel):
    wallet: str = Field(pattern=auth.WALLET_PATTERN)


class RegisterIn(BaseModel):
    wallet: str = Field(pattern=auth.WALLET_PATTERN)
    nonce: str
    signature: str
    name: str = Field(default="", max_length=64)


class SignalIn(BaseModel):
    pair: str
    horizon: str
    direction: str
    confidence: float


@app.get("/health")
def health():
    return {"ok": True, "pairs": config.PAIRS, "horizons": list(config.HORIZONS), "wallets": ["evm", "solana"],
            "scoring_version": scoring.SCORING_VERSION}


@app.post("/agents/challenge")
def challenge(body: ChallengeIn, db: Session = Depends(get_session)):
    _registration_throttle()
    nonce = auth.new_challenge(db, body.wallet)
    return {"nonce": nonce, "message": auth.registration_message(body.wallet, nonce)}


@app.post("/agents", status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_session)):
    wallet = auth.canonical_wallet(body.wallet)
    auth.consume_challenge(db, wallet, body.nonce)
    if not auth.verify(auth.registration_message(wallet, body.nonce), body.signature, wallet):
        raise HTTPException(401, "signature does not match wallet")
    if db.query(Agent).filter_by(wallet=wallet).first():
        raise HTTPException(409, "wallet already registered")
    key = auth.new_api_key()
    agent = Agent(wallet=wallet, api_key_hash=auth.hash_api_key(key), name=body.name)
    db.add(agent)
    db.flush()
    service.grant_starter(db, agent)
    db.add(AuditEvent(agent_id=agent.id, event="registered"))
    db.commit()
    return {"agent_id": agent.id, "api_key": key, "wallet_type": auth.wallet_type(wallet), "starter_credits": config.STARTER_CREDITS}


@app.post("/signals", status_code=201)
def post_signal(body: SignalIn, agent: Agent = Depends(auth.signed_agent), db: Session = Depends(get_session)):
    sig = service.submit_signal(db, agent, body.pair, body.horizon, body.direction, body.confidence)
    return {"signal_id": sig.id, "submitted_at": sig.submitted_at.isoformat(), "resolve_at": sig.resolve_at.isoformat(),
            "price_at_submit": sig.price_at_submit, "hash": sig.hash}


@app.get("/room")
def room(pair: str | None = None, horizon: str | None = None,
         agent: Agent = Depends(auth.signed_agent), db: Session = Depends(get_session)):
    service.require_in_good_standing(agent)
    return {"signals": service.room_listing(db, pair, horizon)}


@app.get("/room/{signal_id}")
def room_read(signal_id: str, agent: Agent = Depends(auth.signed_agent), db: Session = Depends(get_session)):
    return service.read_signal(db, agent, signal_id)


@app.get("/me")
def me(agent: Agent = Depends(auth.signed_agent), db: Session = Depends(get_session)):
    return {
        "agent_id": agent.id, "wallet": agent.wallet, "name": agent.name, "status": agent.status,
        "delisted_at": agent.delisted_at.isoformat() if agent.delisted_at else None,
        "delist_count_12mo": service.delists_last_year(db, agent.id),
        "credits": service.balance(db, agent.id),
        "records": {h: service.agent_record(db, agent.id, h, "90d") for h in config.HORIZONS},
        "all_time": service.agent_record(db, agent.id, None, "all"),
        "signals": service.my_signals(db, agent),
    }


@app.get("/agents/{agent_id}")
def agent_public(agent_id: str, horizon: str | None = None, window: str = Query("90d", pattern="^(30d|90d|all)$"),
                 _: Agent = Depends(auth.signed_agent), db: Session = Depends(get_session)):
    target = db.get(Agent, agent_id)
    if not target:
        raise HTTPException(404, "no such agent")
    return service.public_agent(db, target, horizon, window)


@app.get("/leaderboard")
def leaderboard_view(horizon: str | None = None, pair: str | None = None,
                     window: str = Query("90d", pattern="^(30d|90d|all)$"),
                     _: Agent = Depends(auth.signed_agent), db: Session = Depends(get_session)):
    return {"window": window, "horizon": horizon or "all", "pair": pair or "all",
            "min_resolved": config.MIN_RESOLVED_FOR_RANK, "agents": service.leaderboard(db, horizon, pair, window)}


@app.get("/log/{day}")
def daily_log(day: str, _: Agent = Depends(auth.signed_agent), db: Session = Depends(get_session)):
    try:
        d = dt.date.fromisoformat(day)
    except ValueError:
        raise HTTPException(422, "day must be YYYY-MM-DD")
    return service.daily_log_view(db, d)

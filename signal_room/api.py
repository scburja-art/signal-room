"""The agent API. Every endpoint except registration requires a signed request."""
from __future__ import annotations

import datetime as dt

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import auth, config, service
from .db import Agent, AuditEvent, get_session

app = FastAPI(title="Signal Room", version="0.1", docs_url="/docs", redoc_url=None)


class ChallengeIn(BaseModel):
    wallet: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")


class RegisterIn(BaseModel):
    wallet: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
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
    return {"ok": True, "pairs": config.PAIRS, "horizons": list(config.HORIZONS)}


@app.post("/agents/challenge")
def challenge(body: ChallengeIn, db: Session = Depends(get_session)):
    nonce = auth.new_challenge(db, body.wallet)
    return {"nonce": nonce, "message": auth.registration_message(body.wallet, nonce)}


@app.post("/agents", status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_session)):
    wallet = body.wallet.lower()
    auth.consume_challenge(db, wallet, body.nonce)
    if auth.recover(auth.registration_message(wallet, body.nonce), body.signature) != wallet:
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
    return {"agent_id": agent.id, "api_key": key, "starter_credits": config.STARTER_CREDITS}


@app.post("/signals", status_code=201)
def post_signal(body: SignalIn, agent: Agent = Depends(auth.signed_agent), db: Session = Depends(get_session)):
    sig = service.submit_signal(db, agent, body.pair, body.horizon, body.direction, body.confidence)
    return {"signal_id": sig.id, "submitted_at": sig.submitted_at.isoformat(), "resolve_at": sig.resolve_at.isoformat(),
            "price_at_submit": sig.price_at_submit, "hash": sig.hash}


@app.get("/room")
def room(pair: str | None = None, horizon: str | None = None,
         agent: Agent = Depends(auth.signed_agent), db: Session = Depends(get_session)):
    return {"signals": service.room_listing(db, pair, horizon)}


@app.get("/room/{signal_id}")
def room_read(signal_id: str, agent: Agent = Depends(auth.signed_agent), db: Session = Depends(get_session)):
    return service.read_signal(db, agent, signal_id)


@app.get("/me")
def me(agent: Agent = Depends(auth.signed_agent), db: Session = Depends(get_session)):
    return {
        "agent_id": agent.id, "wallet": agent.wallet, "name": agent.name, "status": agent.status,
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

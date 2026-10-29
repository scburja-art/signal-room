"""The agent API. Every endpoint except registration requires a signed request."""
from __future__ import annotations

import datetime as dt
import threading
import time
from collections import deque

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import auth, config, discovery, scoring, service
from .db import Agent, AuditEvent, get_session

app = FastAPI(
    title=discovery.NAME, version="0.2", summary=discovery.TAGLINE, description=discovery.DESCRIPTION,
    openapi_tags=discovery.OPENAPI_TAGS, docs_url="/docs", redoc_url=None,
    license_info={"name": "MIT"},
)


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@app.get("/llms.txt", response_class=PlainTextResponse, tags=["join"], summary="Agent-readable description of the room and how to join")
def llms_txt(request: Request):
    return discovery.llms_txt(_base_url(request))


@app.get("/.well-known/agent.json", tags=["join"], summary="A2A agent card")
def agent_card(request: Request):
    return discovery.agent_card(_base_url(request))

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


@app.get("/health", tags=["records"])
def health():
    return {"ok": True, "pairs": config.PAIRS, "horizons": list(config.HORIZONS), "wallets": ["evm", "solana"],
            "scoring_version": scoring.SCORING_VERSION}


@app.post("/agents/challenge", tags=["join"], summary="Step 1: get a nonce and the message to sign")
def challenge(body: ChallengeIn, db: Session = Depends(get_session)):
    _registration_throttle()
    nonce = auth.new_challenge(db, body.wallet)
    return {"nonce": nonce, "message": auth.registration_message(body.wallet, nonce)}


@app.post("/agents", status_code=201, tags=["join"], summary="Step 2: register the wallet, receive an API key")
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


@app.post("/signals", status_code=201, tags=["signals"], summary="Post a prediction")
def post_signal(body: SignalIn, agent: Agent = Depends(auth.signed_agent), db: Session = Depends(get_session)):
    sig = service.submit_signal(db, agent, body.pair, body.horizon, body.direction, body.confidence)
    return {"signal_id": sig.id, "submitted_at": sig.submitted_at.isoformat(), "resolve_at": sig.resolve_at.isoformat(),
            "price_at_submit": sig.price_at_submit, "hash": sig.hash}


@app.get("/room", tags=["signals"], summary="Open signals (no direction or confidence)")
def room(pair: str | None = None, horizon: str | None = None,
         agent: Agent = Depends(auth.signed_agent), db: Session = Depends(get_session)):
    service.require_in_good_standing(agent)
    return {"signals": service.room_listing(db, pair, horizon)}


@app.get("/room/{signal_id}", tags=["signals"], summary="Read one signal's body (costs a credit once)")
def room_read(signal_id: str, agent: Agent = Depends(auth.signed_agent), db: Session = Depends(get_session)):
    return service.read_signal(db, agent, signal_id)


@app.get("/me", tags=["records"], summary="Your own record and history")
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


@app.get("/agents/{agent_id}", tags=["records"], summary="Any agent's public record")
def agent_public(agent_id: str, horizon: str | None = None, window: str = Query("90d", pattern="^(30d|90d|all)$"),
                 _: Agent = Depends(auth.signed_agent), db: Session = Depends(get_session)):
    target = db.get(Agent, agent_id)
    if not target:
        raise HTTPException(404, "no such agent")
    return service.public_agent(db, target, horizon, window)


@app.get("/leaderboard", tags=["records"], summary="Ranked agents with confidence figures")
def leaderboard_view(horizon: str | None = None, pair: str | None = None,
                     window: str = Query("90d", pattern="^(30d|90d|all)$"),
                     _: Agent = Depends(auth.signed_agent), db: Session = Depends(get_session)):
    return {"window": window, "horizon": horizon or "all", "pair": pair or "all",
            "min_resolved": config.MIN_RESOLVED_FOR_RANK, "agents": service.leaderboard(db, horizon, pair, window)}


@app.get("/log/{day}", tags=["records"], summary="Daily hash log and Merkle root")
def daily_log(day: str, _: Agent = Depends(auth.signed_agent), db: Session = Depends(get_session)):
    try:
        d = dt.date.fromisoformat(day)
    except ValueError:
        raise HTTPException(422, "day must be YYYY-MM-DD")
    return service.daily_log_view(db, d)

"""Business logic: submitting signals, resolving them, credits, records, the daily log."""
from __future__ import annotations

import datetime as dt
import logging
import threading

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import config, prices, scoring
from .db import Agent, AuditEvent, CreditEntry, DailyLog, Read, Signal, utcnow
from .sealing import merkle_root, new_salt, seal, signal_hash, unseal

log = logging.getLogger("signal_room.service")

# Serializes check-then-debit on credits. One process, so a process lock is enough; a second
# process would need a row lock or a balance table with a CHECK constraint.
_credit_lock = threading.Lock()


# ---------- credits ----------

def balance(db: Session, agent_id: str) -> float:
    total = db.query(func.coalesce(func.sum(CreditEntry.amount), 0.0)).filter_by(agent_id=agent_id).scalar()
    return round(float(total), 4)


def grant_starter(db: Session, agent: Agent) -> None:
    db.add(CreditEntry(agent_id=agent.id, amount=config.STARTER_CREDITS, reason="starter"))


# ---------- signals ----------

def submit_signal(db: Session, agent: Agent, pair: str, horizon: str, direction: str, confidence: float) -> Signal:
    # Delisted agents may still post: that is the only road back. Retired wallets are done.
    if agent.status == "retired":
        raise HTTPException(403, "wallet retired")
    if pair not in config.PAIRS:
        raise HTTPException(422, f"pair must be one of {list(config.PAIRS)}")
    if horizon not in config.HORIZONS:
        raise HTTPException(422, f"horizon must be one of {list(config.HORIZONS)}")
    if direction not in ("long", "short"):
        raise HTTPException(422, "direction must be long or short")
    if not (config.MIN_CONFIDENCE <= confidence <= config.MAX_CONFIDENCE):
        raise HTTPException(422, f"confidence must be between {config.MIN_CONFIDENCE} and {config.MAX_CONFIDENCE}")

    open_exists = db.query(Signal).filter_by(agent_id=agent.id, pair=pair, horizon=horizon, resolved_at=None).first()
    if open_exists:
        raise HTTPException(409, "one open signal per pair per horizon")

    ref = prices.latest(db, pair, config.PRICE_MAX_AGE_SECONDS)
    if ref is None:
        raise HTTPException(503, "no fresh reference price; try again in a minute")

    now = utcnow()
    salt = new_salt()
    sig = Signal(
        agent_id=agent.id, pair=pair, horizon=horizon,
        body_cipher=seal(agent.id, {"direction": direction, "confidence": confidence, "salt": salt}),
        hash=signal_hash(agent.id, pair, horizon, direction, confidence, now.isoformat(), ref.price, salt),
        submitted_at=now, resolve_at=now + dt.timedelta(seconds=config.HORIZONS[horizon]),
        price_at_submit=ref.price,
    )
    db.add(sig)
    try:
        db.commit()
    except IntegrityError:  # lost a race with a concurrent post for the same pair/horizon
        db.rollback()
        raise HTTPException(409, "one open signal per pair per horizon")
    return sig


def resolve_due(db: Session, now: dt.datetime | None = None) -> int:
    """Resolve every signal whose horizon has passed. Returns how many were resolved."""
    now = now or utcnow()
    due = db.query(Signal).filter(Signal.resolved_at.is_(None), Signal.resolve_at <= now).all()
    count = 0
    touched: set[str] = set()
    for sig in due:
        try:
            if _resolve_one(db, sig, now):
                count += 1
                touched.add(sig.agent_id)
        except Exception as e:  # one bad row must never stall resolution for everyone
            db.rollback()
            log.exception("resolve failed for signal %s: %s", sig.id, e)
            try:
                sig = db.get(Signal, sig.id)
                sig.result, sig.resolved_at, sig.credit = "void", now, 0.0
                db.add(AuditEvent(agent_id=sig.agent_id, event="signal_void", detail=f"{sig.id} error={type(e).__name__}"))
                db.commit()
                count += 1
            except Exception:
                db.rollback()
    if touched:
        update_standing(db, touched, now)
    return count


def _resolve_one(db: Session, sig: Signal, now: dt.datetime) -> bool:
    """Score one due signal. Returns False when it is not ready yet (waiting for a price)."""
    ref = prices.nearest(db, sig.pair, sig.resolve_at, config.RESOLVE_TOLERANCE_SECONDS)
    still_waiting = (now - sig.resolve_at).total_seconds() <= config.RESOLVE_TOLERANCE_SECONDS
    if ref is None or (still_waiting and ref.sampled_at < sig.resolve_at):
        # Prefer a sample taken at or after the horizon. Inside the tolerance window, wait for one.
        if still_waiting:
            return False
        if ref is None:
            # The sampler missed this minute (machine asleep, process down). Rebuild it from the
            # exchanges' candles before giving up; void only if that is impossible too.
            age_hours = (now - sig.resolve_at).total_seconds() / 3600
            if age_hours <= config.BACKFILL_WINDOW_HOURS:
                prices.backfill(db, sig.pair, sig.resolve_at - dt.timedelta(minutes=2), sig.resolve_at + dt.timedelta(minutes=2))
                ref = prices.nearest(db, sig.pair, sig.resolve_at, config.RESOLVE_TOLERANCE_SECONDS)
                if ref is None:
                    return False  # feeds unreachable right now (just woke up?); try again next tick
        if ref is None:
            body = unseal(sig.agent_id, sig.body_cipher)
            sig.direction, sig.confidence = body["direction"], body["confidence"]
            sig.result, sig.resolved_at, sig.credit = "void", now, 0.0
            db.add(AuditEvent(agent_id=sig.agent_id, event="signal_void", detail=sig.id))
            db.commit()
            return True
    body = unseal(sig.agent_id, sig.body_cipher)
    direction, confidence = body["direction"], float(body["confidence"])
    if sig.price_at_submit <= 0:
        raise ValueError("bad price_at_submit")
    result, y = scoring.outcome(direction, sig.price_at_submit, ref.price)
    bs = scoring.brier(confidence, y)
    credit = scoring.credit_for(bs, result)
    sig.direction, sig.confidence = direction, confidence
    sig.price_at_resolve = ref.price
    # resolved_at is the horizon itself, so windows are horizon-based and same-batch order is deterministic.
    sig.resolved_at = sig.resolve_at
    sig.result, sig.brier = result, (round(bs, 6) if bs is not None else None)
    sig.net_return = round(scoring.net_return(direction, sig.price_at_submit, ref.price), 6)
    sig.credit = round(credit, 4)
    sig.scoring_version = scoring.SCORING_VERSION
    if sig.credit != 0:
        db.add(CreditEntry(agent_id=sig.agent_id, amount=sig.credit, reason="resolved", signal_id=sig.id))
    db.commit()
    return True


def update_standing(db: Session, agent_ids: set[str], now: dt.datetime | None = None) -> None:
    """After resolutions: delist active agents that have gone bad, reinstate delisted ones that recovered,
    retire wallets delisted three times in a year."""
    now = now or utcnow()
    for aid in agent_ids:
        agent = db.get(Agent, aid)
        if not agent or agent.status == "retired":
            continue
        if agent.status == "active":
            # Only the record since the last delisting counts, so a reinstated agent starts clean.
            q = db.query(Signal).filter(Signal.agent_id == aid, Signal.result.in_(("right", "wrong")))
            if agent.delisted_at:
                q = q.filter(Signal.resolved_at > agent.delisted_at)
            recent = q.order_by(Signal.resolved_at.desc()).limit(config.DELIST_WINDOW).all()
            if scoring.should_delist([s.brier for s in reversed(recent)]):
                agent.delist_count = delists_last_year(db, aid, now) + 1
                # As of the horizon of the signal that tipped it, so "fresh" means resolved after that signal.
                agent.delisted_at = max(s.resolved_at for s in recent)
                agent.status = "retired" if agent.delist_count >= config.RETIRE_AFTER_DELISTS else "delisted"
                db.add(AuditEvent(agent_id=aid, event="delisted", detail=f"count_12mo={agent.delist_count}", created_at=now))
                if agent.status == "retired":
                    db.add(AuditEvent(agent_id=aid, event="retired", detail="", created_at=now))
        elif agent.status == "delisted":
            fresh = db.query(Signal).filter(
                Signal.agent_id == aid, Signal.result.in_(("right", "wrong")),
                Signal.resolved_at > agent.delisted_at,
            ).order_by(Signal.resolved_at).all()
            if len(fresh) >= config.REINSTATE_MIN_RESOLVED and scoring.skill_score([s.brier for s in fresh]) > 0:
                agent.status = "active"
                db.add(AuditEvent(agent_id=aid, event="reinstated", detail=f"n={len(fresh)}", created_at=now))
    db.commit()


# ---------- records ----------

def _resolved_query(db: Session, agent_id: str, horizon: str | None, since: dt.datetime | None):
    q = db.query(Signal).filter(Signal.agent_id == agent_id, Signal.result.in_(("right", "wrong", "flat")))
    if horizon:
        q = q.filter(Signal.horizon == horizon)
    if since:
        q = q.filter(Signal.resolved_at >= since)
    return q.order_by(Signal.resolved_at)


def agent_record(db: Session, agent_id: str, horizon: str | None = None, window: str = "90d") -> dict:
    since = {"30d": 30, "90d": 90}.get(window)
    since_dt = utcnow() - dt.timedelta(days=since) if since else None
    rows = _resolved_query(db, agent_id, horizon, since_dt).all()
    rec = scoring.record([s.brier for s in rows], [s.result for s in rows], [s.net_return for s in rows if s.net_return is not None])
    return {"horizon": horizon or "all", "window": window, **rec.__dict__}


def public_agent(db: Session, agent: Agent, horizon: str | None, window: str) -> dict:
    rec = agent_record(db, agent.id, horizon, window)
    return {"agent_id": agent.id, "name": agent.name, "status": agent.status, "since": agent.created_at.isoformat(), **rec}


def leaderboard(db: Session, horizon: str | None, pair: str | None, window: str, limit: int = 100) -> list[dict]:
    agents = db.query(Agent).filter(Agent.status == "active").all()
    out = []
    for a in agents:
        since = {"30d": 30, "90d": 90}.get(window)
        since_dt = utcnow() - dt.timedelta(days=since) if since else None
        q = _resolved_query(db, a.id, horizon, since_dt)
        if pair:
            q = q.filter(Signal.pair == pair)
        rows = q.all()
        rec = scoring.record([s.brier for s in rows], [s.result for s in rows], [s.net_return for s in rows if s.net_return is not None])
        if rec.ranked:
            out.append({"agent_id": a.id, "name": a.name, **rec.__dict__})
    out.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out[:limit]


# ---------- the room ----------

def room_listing(db: Session, pair: str | None, horizon: str | None, limit: int = 200) -> list[dict]:
    q = db.query(Signal).join(Agent, Agent.id == Signal.agent_id).filter(Signal.resolved_at.is_(None), Agent.status == "active")
    if pair:
        q = q.filter(Signal.pair == pair)
    if horizon:
        q = q.filter(Signal.horizon == horizon)
    rows = q.order_by(Signal.submitted_at.desc()).limit(limit).all()
    out = []
    for s in rows:
        a = db.get(Agent, s.agent_id)
        rec = agent_record(db, s.agent_id, s.horizon, "90d")
        out.append({
            "signal_id": s.id, "agent_id": s.agent_id, "agent_name": a.name if a else "",
            "pair": s.pair, "horizon": s.horizon, "submitted_at": s.submitted_at.isoformat(),
            "resolve_at": s.resolve_at.isoformat(), "hash": s.hash,
            "agent_score": rec["score"], "agent_ranked": rec["ranked"], "agent_n": rec["n"],
        })
    return out


def delists_last_year(db: Session, agent_id: str, now: dt.datetime | None = None) -> int:
    now = now or utcnow()
    return db.query(AuditEvent).filter(
        AuditEvent.agent_id == agent_id, AuditEvent.event == "delisted",
        AuditEvent.created_at >= now - dt.timedelta(days=365),
    ).count()


def require_in_good_standing(agent: Agent) -> None:
    if agent.status != "active":
        raise HTTPException(403, f"agent is {agent.status}: may post signals to earn reinstatement, but cannot read the room")


def read_signal(db: Session, reader: Agent, signal_id: str) -> dict:
    sig = db.get(Signal, signal_id)
    if not sig:
        raise HTTPException(404, "no such signal")
    own = sig.agent_id == reader.id
    if not own:
        require_in_good_standing(reader)
    if not own:
        with _credit_lock:
            already = db.get(Read, (reader.id, signal_id)) is not None
            if not already:
                if sig.resolved_at is None:  # open signals cost a credit; resolved ones are public record
                    bal = balance(db, reader.id)
                    if bal < config.READ_COST:
                        raise HTTPException(402, detail={"message": "insufficient credits", "balance": bal, "cost": config.READ_COST})
                    db.add(CreditEntry(agent_id=reader.id, amount=-config.READ_COST, reason="read", signal_id=sig.id))
                db.add(Read(agent_id=reader.id, signal_id=sig.id))
                db.add(AuditEvent(agent_id=reader.id, event="read", detail=sig.id))
                db.commit()
    body = unseal(sig.agent_id, sig.body_cipher)
    return {
        "signal_id": sig.id, "agent_id": sig.agent_id, "pair": sig.pair, "horizon": sig.horizon,
        "direction": body["direction"], "confidence": body["confidence"],
        "submitted_at": sig.submitted_at.isoformat(), "resolve_at": sig.resolve_at.isoformat(),
        "price_at_submit": sig.price_at_submit, "hash": sig.hash,
        "result": sig.result, "price_at_resolve": sig.price_at_resolve, "brier": sig.brier, "net_return": sig.net_return,
    }


def my_signals(db: Session, agent: Agent, limit: int = 200) -> list[dict]:
    rows = db.query(Signal).filter_by(agent_id=agent.id).order_by(Signal.submitted_at.desc()).limit(limit).all()
    out = []
    for s in rows:
        body = unseal(agent.id, s.body_cipher)
        out.append({
            "signal_id": s.id, "pair": s.pair, "horizon": s.horizon, "direction": body["direction"],
            "confidence": body["confidence"], "submitted_at": s.submitted_at.isoformat(),
            "resolve_at": s.resolve_at.isoformat(), "price_at_submit": s.price_at_submit, "hash": s.hash,
            "result": s.result, "price_at_resolve": s.price_at_resolve, "brier": s.brier,
            "net_return": s.net_return, "credit": s.credit, "scoring_version": s.scoring_version,
        })
    return out


# ---------- daily log ----------

def build_daily_log(db: Session, day: dt.date) -> DailyLog:
    start = dt.datetime(day.year, day.month, day.day)
    end = start + dt.timedelta(days=1)
    rows = db.query(Signal).filter(Signal.submitted_at >= start, Signal.submitted_at < end).order_by(Signal.submitted_at).all()
    root = merkle_root([s.hash for s in rows])
    log = db.get(DailyLog, day.isoformat())
    if log is None:
        log = DailyLog(day=day.isoformat(), merkle_root=root, signal_count=len(rows))
        db.add(log)
    elif log.anchored_tx:
        pass  # once anchored, the stored root is frozen; /log/{day} shows live vs anchored side by side
    else:
        log.merkle_root, log.signal_count = root, len(rows)
    db.commit()
    return log


def daily_log_view(db: Session, day: dt.date) -> dict:
    start = dt.datetime(day.year, day.month, day.day)
    end = start + dt.timedelta(days=1)
    rows = db.query(Signal).filter(Signal.submitted_at >= start, Signal.submitted_at < end).order_by(Signal.submitted_at).all()
    log = db.get(DailyLog, day.isoformat())
    return {
        "day": day.isoformat(),
        "merkle_root": merkle_root([s.hash for s in rows]),
        "anchored_root": log.merkle_root if log else None,
        "anchored_tx": log.anchored_tx if log else None,
        "signals": [{"signal_id": s.id, "agent_id": s.agent_id, "pair": s.pair, "horizon": s.horizon,
                     "submitted_at": s.submitted_at.isoformat(), "hash": s.hash} for s in rows],
    }

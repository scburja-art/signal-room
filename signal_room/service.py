"""Business logic: submitting signals, resolving them, credits, records, the daily log."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import config, kalshi, prices, scoring
from .db import Agent, AuditEvent, CreditEntry, DailyLog, ReferencePrice, Read, Signal, utcnow
from .sealing import leaf_hash, merkle_root, new_salt, priced_hash, priced_leaf, seal, signal_hash, unseal

log = logging.getLogger("signal_room.service")


def event(kind: str, **fields) -> None:
    """One machine-readable line per thing that happened, for the live feed. Never carries an open
    call's direction or confidence: those appear only once the call has closed."""
    log.info("EVENT %s", json.dumps({"kind": kind, **fields}, default=str))


def _name(db: Session, agent_id: str) -> str:
    a = db.get(Agent, agent_id)
    return a.name if a and a.name else agent_id

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

def _may_post(db: Session, agent: Agent, pair: str, horizon: str) -> None:
    """The two rules every call faces, plain or priced."""
    open_exists = db.query(Signal).filter_by(agent_id=agent.id, pair=pair, horizon=horizon, resolved_at=None).first()
    if open_exists:
        raise HTTPException(409, "one open signal per pair per horizon")

    # Copy rule: after reading another agent's open call on a coin, you may not post on that coin
    # (any time window) until that call closes. Otherwise a paid read could be reposted as your own
    # call and build your record on someone else's work.
    read_open = (db.query(Signal).join(Read, Read.signal_id == Signal.id)
                 .filter(Read.agent_id == agent.id, Signal.agent_id != agent.id,
                         Signal.pair == pair, Signal.resolved_at.is_(None))
                 .order_by(Signal.resolve_at.desc()).first())
    if read_open:
        raise HTTPException(409, f"you read an open {pair} call; you may post on {pair} after it closes at {read_open.resolve_at.isoformat()}Z")


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

    _may_post(db, agent, pair, horizon)

    # The sampler must be alive: the start price will be its next sample.
    if prices.latest(db, pair, config.PRICE_MAX_AGE_SECONDS) is None:
        raise HTTPException(503, "no fresh reference price; try again in a minute")

    now = utcnow()
    salt = new_salt()
    # The scored window starts at the first reference sample taken after this post (filled on the
    # next tick), so a bot that watches live prices cannot cash in a move the room has not seen yet.
    # resolve_at is provisional until then; it becomes start + horizon.
    sig = Signal(
        agent_id=agent.id, pair=pair, horizon=horizon,
        body_cipher=seal(agent.id, {"direction": direction, "confidence": confidence, "salt": salt}),
        hash=signal_hash(agent.id, pair, horizon, direction, confidence, now.isoformat(), None, salt),
        submitted_at=now, resolve_at=now + dt.timedelta(seconds=config.HORIZONS[horizon]),
        price_at_submit=None, start_at=None, hash_version=3,
    )
    db.add(sig)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        if "ux_signal_one_open" in str(e.orig) or "UNIQUE" in str(e.orig).upper():
            raise HTTPException(409, "one open signal per pair per horizon")   # lost a race with a concurrent post
        log.error("signal insert failed: %s", e.orig)   # a schema problem, never to be reported as 'already open'
        raise HTTPException(500, "the room could not store the signal; see the server log")
    event("posted", agent=agent.name or agent.id, pair=pair, horizon=horizon, closes=sig.resolve_at.isoformat())
    return sig


def fill_start_prices(db: Session, now: dt.datetime | None = None) -> int:
    """Give every call still without a start price the first reference sample taken after its post.
    Samples are stamped at the start of the minute they were fetched in, so a sample stamped at or
    after the post was fetched after it. Returns how many were filled."""
    now = now or utcnow()
    filled = 0
    for sig in db.query(Signal).filter(Signal.kind.is_(None), Signal.price_at_submit.is_(None),
                                       Signal.resolved_at.is_(None)).all():
        ref = (db.query(ReferencePrice).filter(ReferencePrice.pair == sig.pair, ReferencePrice.sampled_at >= sig.submitted_at)
               .order_by(ReferencePrice.sampled_at).first())
        if ref is None:
            continue
        sig.price_at_submit, sig.start_at = ref.price, ref.sampled_at
        sig.resolve_at = ref.sampled_at + dt.timedelta(seconds=config.HORIZONS[sig.horizon])
        filled += 1
        # The call's window is now set. No direction here: open calls stay sealed in every log.
        event("started", agent=_name(db, sig.agent_id), pair=sig.pair, horizon=sig.horizon, signal_id=sig.id,
              start=ref.price, closes=sig.resolve_at.isoformat())
    if filled:
        db.commit()
    return filled


def resolve_due(db: Session, now: dt.datetime | None = None) -> int:
    """Resolve every signal whose horizon has passed. Returns how many were resolved."""
    now = now or utcnow()
    fill_start_prices(db, now)
    fill_waiting(db, now)
    due = db.query(Signal).filter(Signal.kind.is_(None), Signal.resolved_at.is_(None), Signal.resolve_at <= now).all()
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
    priced, agents = settle_priced(db, now)
    touched |= agents
    if touched:
        update_standing(db, touched, now)
    return count + priced


def _resolve_one(db: Session, sig: Signal, now: dt.datetime) -> bool:
    """Score one due signal. Returns False when it is not ready yet (waiting for a price)."""
    if sig.price_at_submit is None:
        # No sample since the post (the sampler was down). Rebuild that minute from exchange candles,
        # then start the window there; void only if that is impossible too.
        age_hours = (now - sig.submitted_at).total_seconds() / 3600
        if age_hours <= config.BACKFILL_WINDOW_HOURS:
            prices.backfill(db, sig.pair, sig.submitted_at, sig.submitted_at + dt.timedelta(minutes=3))
            if fill_start_prices(db, now):
                db.refresh(sig)
        if sig.price_at_submit is None:
            if age_hours <= config.BACKFILL_WINDOW_HOURS:
                return False
            body = unseal(sig.agent_id, sig.body_cipher)
            sig.direction, sig.confidence = body["direction"], body["confidence"]
            sig.result, sig.resolved_at, sig.credit = "void", now, 0.0
            db.add(AuditEvent(agent_id=sig.agent_id, event="signal_void", detail=f"{sig.id} no start price"))
            db.commit()
            event("scored", agent=_name(db, sig.agent_id), pair=sig.pair, horizon=sig.horizon, result="void")
            return True
        if sig.resolve_at > now:
            return False   # the window moved to the start sample; not due yet
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
            event("scored", agent=_name(db, sig.agent_id), pair=sig.pair, horizon=sig.horizon, result="void")
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
    sig.scoring_version = scoring.SCORING_VERSION if sig.hash_version == 3 else 2   # posted under the old start rule
    if sig.credit != 0:
        db.add(CreditEntry(agent_id=sig.agent_id, amount=sig.credit, reason="resolved", signal_id=sig.id))
    db.commit()
    # After resolution the call is public record, so it may be shown.
    event("scored", agent=_name(db, sig.agent_id), pair=sig.pair, horizon=sig.horizon, direction=direction,
          confidence=confidence, start=sig.price_at_submit, end=ref.price, result=result, credit=sig.credit,
          opened=(sig.start_at or sig.submitted_at).isoformat(), closed=sig.resolve_at.isoformat())
    return True


# ---------- priced calls (rule set 4) ----------
#
# A priced call is one side of a venue market, with the most the agent would pay. The room reads the
# price itself the moment the call lands, fills the call only if a small real order could have been,
# and scores it against what it cost (price + fee) using the venue's own result. The room witnesses;
# it never places an order and never touches money.

def decide_fill(ask: float | None, size: float, limit: float, seconds_left: float) -> tuple[str, str]:
    """('filled', '') or ('unfilled', why). Decided from the price alone, before any result exists."""
    if ask is None:
        return "unfilled", "nothing on offer"
    if seconds_left < config.PRICED_MIN_SECONDS_LEFT:
        return "unfilled", "under a minute left"
    if not (config.PRICED_MIN_PRICE <= ask <= config.PRICED_MAX_PRICE):
        return "unfilled", "price outside 5c-95c"
    if ask > limit + 1e-9:
        return "unfilled", "price above the limit"
    if size < config.PRICED_MIN_SIZE:
        return "unfilled", "under 10 on offer"
    return "filled", ""


# Serializes the closing of a priced call and the deciding of its fill. The minute tick and a bot's next
# post can both reach the same call in the same instant; without this both paid its credits (review
# finding 2026-11-11). One process, so a process lock is enough; the close is also claimed with a
# guarded UPDATE so a second process cannot pay again either. Never held across a read of the venue.
_settle_lock = threading.Lock()

DECIDED = "decided"   # what an open row shows once its fill is decided: that it is, never which way


def _seen(sig: Signal, body: dict) -> dict:
    """What the room saw at the venue for this call, and what it decided: status (filled | unfilled |
    None while waiting), reason, ask, bid, size, bar, fade_bar. While the call is open all of it lives
    only in the sealed body (the price would give the side away, and whether a one-sided market filled
    says the side too); at close it is copied onto the row."""
    if sig.resolved_at is None:
        return body.get("fill") or {"status": None, "reason": "", "ask": None, "bid": None, "size": None,
                                    "bar": None, "fade_bar": None}
    # Closed: the row is the record the scores are computed from, so the row is what is shown and checked.
    return {"status": sig.fill, "reason": sig.fill_reason or "", "ask": sig.fill_ask, "bid": sig.fill_bid,
            "size": sig.fill_size, "bar": sig.bar, "fade_bar": sig.fade_bar}


def fill_record(sig: Signal, body: dict) -> dict:
    """What the room saw at the venue for this call, exactly as the day's leaf commits to it."""
    seen = _seen(sig, body)
    return {"fill": seen["status"], "reason": seen["reason"] or "", "at": sig.fill_at.isoformat() if sig.fill_at else None,
            "ask": seen["ask"], "bid": seen["bid"], "size": seen["size"], "source": sig.fill_source,
            "bar": seen["bar"], "fade_bar": seen["fade_bar"]}


def _set_fill(sig: Signal, body: dict, status: str, reason: str, at: dt.datetime, ask: float | None,
              bid: float | None, size: float, source: str, closes: dt.datetime) -> None:
    """Record the fill decision. The row shows only that a decision was made, when, and how; which way
    it went and the prices go into the sealed body."""
    filled = status == "filled"
    body["fill"] = {"status": status, "reason": reason or "", "ask": ask, "bid": bid, "size": size,
                    "bar": kalshi.cost(ask) if filled else None, "fade_bar": kalshi.fade_bar(bid) if filled else None}
    sig.body_cipher = seal(sig.agent_id, body)
    sig.fill, sig.fill_at, sig.fill_source = DECIDED, at, source
    sig.resolve_at = closes                       # a priced call closes when its market does
    sig.start_at = at                             # its window in the betting account: from the look to the market's close
    sig.leaf = priced_leaf(sig.hash, body["salt"], fill_record(sig, body))


def submit_priced(db: Session, agent: Agent, venue: str, market: str, side: str, limit: float,
                  confidence: float | None) -> Signal:
    if agent.status == "retired":
        raise HTTPException(403, "wallet retired")
    if venue != kalshi.VENUE:
        raise HTTPException(422, f"venue must be {kalshi.VENUE}")
    parsed = kalshi.parse_ticker(market)
    if not parsed:
        raise HTTPException(422, f"market must be one of Kalshi's 15-minute markets on {list(config.PAIRS)}, e.g. KXBTC15M-...")
    series, pair, horizon = parsed
    if side not in ("yes", "no"):
        raise HTTPException(422, "side must be yes or no")
    # Four decimals is what the hash commits to; the fill is decided on exactly that number.
    limit = round(limit, 4)
    confidence = None if confidence is None else round(confidence, 4)
    if not (0.0 < limit < 1.0):
        raise HTTPException(422, "limit is the most you would pay per contract, in dollars: between 0 and 1")
    if confidence is not None and not (limit <= confidence <= config.MAX_CONFIDENCE):
        raise HTTPException(422, "confidence must be at least the limit (nobody buys what they think is overpriced), "
                                 f"at most {config.MAX_CONFIDENCE}; omit it to claim nothing beyond the price")

    # The agent's call on the last window may be waiting only for this minute's tick; settle it now so
    # one-open-call-per-coin never delays the next call (a delay would change the price it gets).
    _, settled = settle_priced(db, utcnow(), agent_id=agent.id, pair=pair)
    if settled:
        update_standing(db, settled)
        db.refresh(agent)
        if agent.status == "retired":             # the call just settled was the one that retired the wallet
            raise HTTPException(403, "wallet retired")
    _may_post(db, agent, pair, horizon)
    db.commit()   # hand the database connection back before the look at the venue, which can take seconds

    # The stamp comes first, then the look: the price is one the room read after the call landed.
    now = utcnow()
    quote, read_at = None, None
    try:
        kalshi.check_fee_rule(series)
        quote = kalshi.quote(kalshi.fetch_market(market), side)
        read_at = max(utcnow(), now)
    except kalshi.NoSuchMarket:
        raise HTTPException(422, "Kalshi has no such market")
    except kalshi.VenueUnreachable as e:
        log.warning("priced call on %s: live read failed (%s); the fill will come from that minute's history", market, e)
    except kalshi.VenueError as e:
        # Kalshi answered and the room cannot use the answer. Taking the call would park it for nothing.
        log.error("priced call on %s refused: Kalshi's answer cannot be used (%s)", market, e)
        raise HTTPException(503, "the room cannot read that market just now; nothing was recorded, try again later")
    if quote is not None and not (quote.open and quote.open_time <= now < quote.close_time):
        raise HTTPException(422, "that market is not open")

    body = {"side": side, "limit": limit, "confidence": confidence, "salt": new_salt()}
    sig = Signal(
        agent_id=agent.id, pair=pair, horizon=horizon, kind="priced", venue=venue, market=market,
        body_cipher=seal(agent.id, body),
        hash=priced_hash(agent.id, venue, market, side, limit, confidence, now.isoformat(), body["salt"]),
        submitted_at=now, hash_version=4,
        resolve_at=quote.close_time if quote else now + dt.timedelta(seconds=config.HORIZONS[horizon]),   # provisional if unread
    )
    if quote is not None:
        status, why = decide_fill(quote.ask, quote.size, limit, (quote.close_time - read_at).total_seconds())
        _set_fill(sig, body, status, why, read_at, quote.ask, quote.bid, quote.size, "live", quote.close_time)
    db.add(sig)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        if "ux_signal_one_open" in str(e.orig) or "UNIQUE" in str(e.orig).upper():
            raise HTTPException(409, "one open signal per pair per horizon")
        log.error("priced signal insert failed: %s", e.orig)
        raise HTTPException(500, "the room could not store the signal; see the server log")
    sig.seen = _seen(sig, body)   # for the author's receipt only; not a column
    # No side, no price, and not which way the fill went: any of them could give an open call away.
    event("priced", agent=agent.name or agent.id, pair=pair, horizon=horizon, market=market, signal_id=sig.id,
          fill=sig.fill or "waiting", closes=sig.resolve_at.isoformat())
    return sig


def fill_from_history(db: Session, sig: Signal, now: dt.datetime) -> bool:
    """For a priced call the room could not read live: decide its fill from the venue's record of that
    minute, at the worst price the side was offered at in it. Returns True once the fill is decided.
    Raises VenueUnreachable when Kalshi cannot be reached, so the caller can stop asking for this tick."""
    if now < sig.submitted_at.replace(second=0, microsecond=0) + dt.timedelta(seconds=70):
        return False                               # that minute's record is not complete yet
    body = unseal(sig.agent_id, sig.body_cipher)
    decided = None                                 # (status, why, ask, bid, size, closes)
    try:
        kalshi.check_fee_rule(kalshi.parse_ticker(sig.market)[0])
        quote = kalshi.quote(kalshi.fetch_market(sig.market), body["side"])
        if not (quote.open_time <= sig.submitted_at < quote.close_time):
            # Nothing to look up: no minute of that market holds the call.
            decided = ("unfilled", "market was not open", None, None, 0.0, max(quote.close_time, sig.submitted_at))
        else:
            minute = kalshi.fetch_minute(sig.market, sig.submitted_at)
            if minute is None:
                return False
            _, ask_high, bid_low, volume = kalshi.minute_range(minute, body["side"])
            ask = ask_high if 0.0 < ask_high < 1.0 else None
            bid = bid_low if 0.0 < bid_low < 1.0 else None
            status, why = decide_fill(ask, volume, float(body["limit"]), (quote.close_time - sig.submitted_at).total_seconds())
            decided = (status, why, ask, bid, volume, quote.close_time)
    except kalshi.NoSuchMarket:
        # Posted while Kalshi could not be read, on a market that turns out not to exist.
        decided = ("unfilled", "no such market", None, None, 0.0, sig.resolve_at)
    except kalshi.VenueUnreachable:
        raise
    except kalshi.VenueError as e:
        log.warning("priced call %s: Kalshi's record of its minute cannot be used (%s)", sig.id, e)
        return False
    status, why, ask, bid, size, closes = decided
    with _settle_lock:
        db.rollback()                              # end any open read so the row is seen as it is now
        db.refresh(sig)
        if sig.fill is not None or sig.resolved_at is not None:
            return sig.fill is not None            # another thread decided it meanwhile
        _set_fill(sig, body, status, why, sig.submitted_at, ask, bid, size, "history", closes)
        db.commit()
    event("priced", agent=_name(db, sig.agent_id), pair=sig.pair, horizon=sig.horizon, market=sig.market,
          signal_id=sig.id, fill=sig.fill, closes=sig.resolve_at.isoformat())
    return True


def _each_priced(db: Session, rows: list[Signal], step, what: str) -> None:
    """Run step(sig) for each row. No answer at all from Kalshi ends the asking for this tick (the next
    row would only wait out the same timeout); anything else, a busy answer for one market or a bad
    row, is that row's problem alone and the rest go on."""
    for sig in rows:
        try:
            step(sig)
        except kalshi.VenueOffline as e:
            db.rollback()
            log.warning("Kalshi cannot be reached (%s); %s until the next tick", e, what)
            break
        except kalshi.VenueUnreachable as e:
            db.rollback()
            log.info("priced call %s: Kalshi did not answer for its market (%s); it waits", sig.id, e)
        except Exception as e:   # one bad row must not stop the rest, and a real call is never voided over a passing error
            db.rollback()
            log.exception("priced call %s failed this tick: %s", sig.id, type(e).__name__)


def fill_waiting(db: Session, now: dt.datetime | None = None) -> int:
    """Decide, from history, every priced call still waiting for a fill. Returns how many were decided."""
    now = now or utcnow()
    done = []
    rows = db.query(Signal).filter(Signal.kind == "priced", Signal.fill.is_(None), Signal.resolved_at.is_(None)).all()
    _each_priced(db, rows, lambda sig: done.append(1) if fill_from_history(db, sig, now) else None, "waiting calls stay waiting")
    return len(done)


def settle_priced(db: Session, now: dt.datetime | None = None, agent_id: str | None = None,
                  pair: str | None = None) -> tuple[int, set[str]]:
    """Score every priced call whose market has closed. Returns (how many closed, the agents touched)."""
    now = now or utcnow()
    q = db.query(Signal).filter(Signal.kind == "priced", Signal.resolved_at.is_(None), Signal.resolve_at <= now)
    if agent_id:
        q = q.filter(Signal.agent_id == agent_id)
    if pair:
        q = q.filter(Signal.pair == pair)
    touched: list[str] = []
    _each_priced(db, q.all(), lambda sig: touched.append(sig.agent_id) if _settle_one(db, sig, now) else None,
                 "closed calls wait for their result")
    return len(touched), set(touched)


def _settle_one(db: Session, sig: Signal, now: dt.datetime) -> bool:
    """Close one priced call whose market has closed. False while it cannot be closed yet. Raises
    VenueUnreachable while Kalshi cannot be reached: a filled call then waits, however long."""
    if sig.fill is None:
        # No price yet. The venue's history is asked for half an hour; after that there is nothing to
        # score, whatever stood in the way (the deadline holds even if every look fails).
        give_up = (now - sig.submitted_at).total_seconds() > config.PRICED_FILL_WAIT_SECONDS
        try:
            decided = fill_from_history(db, sig, now)
        except Exception:
            if not give_up:
                raise
            db.rollback()
            decided = False
        if not decided and not give_up:
            return False
        if decided and sig.resolve_at > now:
            return False                           # the market's real close is later than the provisional one
    was_decided = sig.fill is not None
    body = unseal(sig.agent_id, sig.body_cipher)
    side, stated, seen = body["side"], body.get("confidence"), _seen(sig, body)
    since_close = (now - sig.resolve_at).total_seconds()

    scored = None                                  # (outcome, confidence, brier, credit, profit)
    if not was_decided:
        verdict, note = "void", "no price could be established"
    elif seen["status"] == "unfilled":
        verdict, note = "unfilled", ""
    else:
        # A filled call is a real call. It is closed only on an answer from Kalshi: its result, or, once
        # the deadline has passed, Kalshi saying there still is none. Unreachable or unusable: it waits.
        try:
            outcome = kalshi.result(kalshi.fetch_market(sig.market))
        except kalshi.NoResult:
            if since_close <= config.PRICED_ODD_RESULT_SECONDS:
                return False                       # one odd answer is not the last word
            outcome = "none"
        except kalshi.VenueUnreachable:
            raise
        except kalshi.VenueError as e:
            log.warning("priced call %s: Kalshi's record of the result cannot be used (%s); it waits", sig.id, e)
            return False
        if outcome is None and since_close / 3600 <= config.BACKFILL_WINDOW_HOURS:
            return False                           # not finalised yet; try again next tick
        if outcome not in ("yes", "no"):
            verdict, note = "void", "the venue gave no yes-or-no result"
        else:
            bar = seen["bar"]
            y = 1.0 if outcome == side else 0.0
            # No stated chance claims the price and no more. Neither does one below what the call cost:
            # nobody buys at 52c believing 50%, and scoring it as said would pay a bot with no edge.
            confidence = bar if stated is None else max(float(stated), bar)
            verdict, note = ("right" if y else "wrong"), ""
            bs = scoring.brier(confidence, y)
            scored = (outcome, confidence, round(bs, 6), round(scoring.credit_for(bs, verdict, bar), 4), round(y - bar, 6))

    closed_at = now if verdict == "void" else sig.resolve_at
    with _settle_lock:
        db.rollback()                              # end any open read so the row is seen as it is now
        db.refresh(sig)
        if sig.resolved_at is not None or (sig.fill is not None) != was_decided:
            return False                           # closed, or its fill decided, by another thread meanwhile
        # The close is claimed in the database itself: whoever's UPDATE finds the row still open closes it.
        claimed = (db.query(Signal).filter(Signal.id == sig.id, Signal.resolved_at.is_(None))
                   .update({Signal.resolved_at: closed_at}, synchronize_session=False))
        if claimed != 1:
            db.rollback()
            return False
        # Only now, as the call closes, do its side, its fill and what the room saw go onto the row.
        sig.result, sig.resolved_at = verdict, closed_at
        sig.side, sig.limit_price = side, float(body["limit"])
        sig.direction = "long" if side == "yes" else "short"   # these markets ask "will it finish higher?"
        sig.fill = seen["status"]
        sig.fill_reason, sig.fill_ask, sig.fill_bid, sig.fill_size = seen["reason"] or None, seen["ask"], seen["bid"], seen["size"]
        sig.bar, sig.fade_bar = seen["bar"], seen["fade_bar"]
        sig.confidence, sig.credit = stated, 0.0
        fields = {"reason": seen["reason"]} if verdict == "unfilled" else {}
        if scored:
            outcome, confidence, sig.brier, sig.credit, sig.net_return = scored   # net_return: profit per contract, in dollars
            sig.confidence, sig.venue_result = confidence, outcome
            sig.scoring_version = scoring.SCORING_VERSION
            if sig.credit != 0:
                db.add(CreditEntry(agent_id=sig.agent_id, amount=sig.credit, reason="resolved", signal_id=sig.id))
            fields = {"confidence": confidence, "bar": sig.bar, "venue_result": outcome, "credit": sig.credit,
                      "opened": sig.fill_at.isoformat(), "closed": sig.resolve_at.isoformat()}
        if verdict == "void":
            db.add(AuditEvent(agent_id=sig.agent_id, event="signal_void", detail=f"{sig.id} {note}"))
        db.commit()
    # Closed, so public record: the side may be shown now.
    event("scored", agent=_name(db, sig.agent_id), pair=sig.pair, horizon=sig.horizon, priced=True,
          market=sig.market, side=side, result=verdict, **fields)
    return True


def _briers_and_refs(rows: list[Signal]) -> tuple[list[float], list[float]]:
    """Each right/wrong call's Brier and the Brier of saying exactly what its bar said."""
    # The reference is rounded as the stored Brier is, so a call that claimed nothing is worth exactly zero.
    return ([s.brier for s in rows],
            [round(scoring.ref_brier(s.bar if s.bar is not None else scoring.EVEN_BAR, 1.0 if s.result == "right" else 0.0), 6)
             for s in rows])


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
            if scoring.should_delist(*_briers_and_refs(list(reversed(recent)))):
                agent.delist_count = delists_last_year(db, aid, now) + 1
                # As of the horizon of the signal that tipped it, so "fresh" means resolved after that signal.
                agent.delisted_at = max(s.resolved_at for s in recent)
                agent.status = "retired" if agent.delist_count >= config.RETIRE_AFTER_DELISTS else "delisted"
                db.add(AuditEvent(agent_id=aid, event="delisted", detail=f"count_12mo={agent.delist_count}", created_at=now))
                if agent.status == "retired":
                    db.add(AuditEvent(agent_id=aid, event="retired", detail="", created_at=now))
                event(agent.status, agent=agent.name or aid)
        elif agent.status == "delisted":
            fresh = db.query(Signal).filter(
                Signal.agent_id == aid, Signal.result.in_(("right", "wrong")),
                Signal.resolved_at > agent.delisted_at,
            ).order_by(Signal.resolved_at).all()
            # Clearly above zero: calls that claim nothing score zero to within rounding, and must not count as skill.
            if len(fresh) >= config.REINSTATE_MIN_RESOLVED and scoring.skill_score(*_briers_and_refs(fresh)) > 1e-9:
                agent.status = "active"
                db.add(AuditEvent(agent_id=aid, event="reinstated", detail=f"n={len(fresh)}", created_at=now))
                event("reinstated", agent=agent.name or aid)
    db.commit()


# ---------- records ----------

def _resolved_query(db: Session, agent_id: str, horizon: str | None, since: dt.datetime | None):
    # Every call that took a stake in the betting account and has closed: scored ones, and flats, voids
    # and priced calls that went unfilled (their stake only comes back).
    q = db.query(Signal).filter(Signal.agent_id == agent_id, Signal.result.in_(("right", "wrong", "flat", "unfilled", "void")))
    if horizon:
        q = q.filter(Signal.horizon == horizon)
    if since:
        q = q.filter(Signal.resolved_at >= since)
    return q.order_by(Signal.resolved_at)


_controls: tuple[float, frozenset[str]] | None = None    # (when read, ids)


def control_ids(db: Session | None = None) -> frozenset[str]:
    """The room's own yardstick bots, by agent id: the ids the operator listed, or else the agents named in
    the seed bots' own identity files in this room's state directory. Read at most once a minute. Only
    the id is taken from a file; nothing else in it is kept or logged. A file that is missing or cannot
    be read simply names nobody: that bot then counts as an ordinary agent, the cautious side."""
    global _controls
    now = time.monotonic()
    if _controls is not None and now - _controls[0] < config.CONTROL_REFRESH_SECONDS:
        return _controls[1]
    ids = set(config.CONTROL_IDS)
    if not ids:
        for name in config.CONTROL_STATE_FILES:
            try:
                with open(os.path.join(config.AGENT_STATE_DIR, name)) as f:
                    agent_id = json.load(f).get("agent_id")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(agent_id, str) and agent_id:
                ids.add(agent_id)
    _controls = (now, frozenset(ids))
    return _controls[1]


def _id_in(name: str) -> str | None:
    """The agent id in one of the room's own identity files. Only the id is taken; a file that is missing
    or cannot be read names nobody."""
    try:
        with open(os.path.join(config.AGENT_STATE_DIR, name)) as f:
            agent_id = json.load(f).get("agent_id")
    except (OSError, ValueError, AttributeError):
        return None
    return agent_id if isinstance(agent_id, str) and agent_id else None


_closed: tuple[float, dict] | None = None


def closed_ids() -> dict[str, str]:
    """Identities that were closed and carried on under another: old agent id -> the id it continues as.
    Read at most once a minute. A pair counts only when both ids are known and differ; anything less
    leaves the old identity an ordinary agent, the cautious side."""
    global _closed
    now = time.monotonic()
    if _closed is not None and now - _closed[0] < config.CONTROL_REFRESH_SECONDS:
        return _closed[1]
    pairs = list(config.CLOSED_IDS) or [(_id_in(old), _id_in(new)) for old, new in config.CLOSED_STATE_FILES]
    out = {old: new for old, new in pairs if old and new and old != new}
    _closed = (now, out)
    return out


def _as_closed(db: Session, rec: dict, successor_id: str) -> dict:
    """A closed identity keeps every number, but it is no longer in the running: no verdict, no next
    step, and the name of the identity that carries on. Unknown successor: left as it was."""
    successor = db.get(Agent, successor_id)
    if successor is None:
        return rec
    rec.update(verdict="closed", next_verdict=None, tries_to_next=None, days_to_next=None,
               continued_as=successor.name, continued_as_id=successor.id)
    return rec


def _as_control(rec: dict) -> dict:
    """A yardstick's record keeps every number (its luck odds are the warning light), but it is not in the
    running for a verdict: it is labelled a control, with no next step."""
    rec.update(verdict="control", next_verdict=None, tries_to_next=None, days_to_next=None)
    return rec


def board_size(db: Session, since: dt.datetime | None) -> int:
    """How many agents have enough scored calls in the window to have been ranked: the field a reader
    picks the best of. An agent that has since been delisted or retired still counts (approved by Steven
    2026-11-11): an owner who ran twenty, kept the luckiest and had the rest thrown out has still picked
    the best of twenty. The room's own yardstick bots are not in that field. At least 1 (the agent itself)."""
    q = db.query(Signal.agent_id).filter(Signal.result.in_(("right", "wrong")))
    controls = control_ids(db)
    if controls:
        q = q.filter(Signal.agent_id.notin_(controls))
    if since:
        q = q.filter(Signal.resolved_at >= since)
    counts = q.group_by(Signal.agent_id).having(func.count(Signal.id) >= config.MIN_RESOLVED_FOR_RANK).all()
    return max(1, len(counts))


def _window(s: Signal) -> tuple[dt.datetime, dt.datetime]:
    """A call's scored window: when its stake leaves the betting account and when it comes back."""
    return (s.start_at or s.submitted_at, s.resolved_at or s.resolve_at)


def _pending_query(db: Session, agent_id: str, horizon: str | None):
    """The agent's calls still open, whose stakes are out. Every open call counts, filled or not: whether
    an open priced call filled is sealed, so it must not show in the agent's public record either. An
    unfilled call's stake simply comes back when its market closes."""
    q = db.query(Signal).filter(Signal.agent_id == agent_id, Signal.resolved_at.is_(None))
    q = q.filter(Signal.horizon == horizon) if horizon else q
    return q.order_by(Signal.submitted_at, Signal.id)


def _record(rows: list[Signal], slices: int = 1, now: dt.datetime | None = None, competitors: int = 1,
            pending: list[Signal] | None = None) -> dict:
    windows = [_window(s) for s in rows]
    clusters = scoring.cluster_ids([(s.start_at or s.submitted_at, s.resolve_at) for s in rows])
    plain = [s for s in rows if s.kind is None]
    long_returns = [(s.price_at_resolve - s.price_at_submit) / s.price_at_submit - config.ROUND_TRIP_COST
                    for s in plain if s.net_return is not None and s.price_at_resolve and s.price_at_submit]
    # A plain call's bar is an even bet (None). A priced call's is what it cost.
    rec = scoring.record([s.brier for s in rows], [s.result for s in rows],
                         [s.net_return for s in plain if s.net_return is not None], clusters, slices, long_returns,
                         competitors, [s.bar for s in rows], [s.fade_bar for s in rows], windows,
                         [s.start_at or s.submitted_at for s in pending or []],
                         [(s.submitted_at, s.id) for s in list(rows) + list(pending or [])])
    out = rec.__dict__.copy()
    # Priced calls are bought, not traded 1:1: what they made is profit per contract, not a compounded return.
    bought = [s.net_return for s in rows if s.kind == "priced" and s.result in ("right", "wrong")]
    out["profit_per_contract"] = round(sum(bought) / len(bought), 4) if bought else None
    # Tries per day so far turns the projected tries into days.
    out["days_to_next"] = None
    if rec.tries_to_next is not None and rows:
        span_days = max(1.0, ((now or utcnow()) - min(s.submitted_at for s in rows)).total_seconds() / 86400)
        out["days_to_next"] = round(rec.tries_to_next / (rec.tries / span_days), 1) if rec.tries else None
    return out


def _since(window: str) -> dt.datetime | None:
    days = {"30d": 30, "90d": 90}.get(window)
    return utcnow() - dt.timedelta(days=days) if days else None


def _slices(db: Session, agent_id: str, since: dt.datetime | None, horizon: str | None, pair: str | None) -> int:
    """How many slices this view is one of: horizons the agent uses when filtered by horizon, times
    coins when filtered by coin."""
    def distinct(col) -> int:
        q = db.query(func.count(func.distinct(col))).filter(Signal.agent_id == agent_id, Signal.result.in_(("right", "wrong")))
        if since:
            q = q.filter(Signal.resolved_at >= since)
        return max(1, q.scalar() or 1)
    return (distinct(Signal.horizon) if horizon else 1) * (distinct(Signal.pair) if pair else 1)


def agent_record(db: Session, agent_id: str, horizon: str | None = None, window: str = "90d") -> dict:
    since = _since(window)
    rows = _resolved_query(db, agent_id, horizon, since).all()
    rec = {"horizon": horizon or "all", "window": window,
           **_record(rows, _slices(db, agent_id, since, horizon, None), competitors=board_size(db, since),
                     pending=_pending_query(db, agent_id, horizon).all())}
    if agent_id in control_ids(db):
        return _as_control(rec)
    return _as_closed(db, rec, closed_ids()[agent_id]) if agent_id in closed_ids() else rec


def public_agent(db: Session, agent: Agent, horizon: str | None, window: str) -> dict:
    rec = agent_record(db, agent.id, horizon, window)
    if agent.status != "active":
        rec["verdict"] = agent.status   # delisted or retired outranks anything the record says
    return {"agent_id": agent.id, "name": agent.name, "status": agent.status, "since": agent.created_at.isoformat(), **rec}


def leaderboard(db: Session, horizon: str | None, pair: str | None, window: str, limit: int = 100) -> list[dict]:
    agents = db.query(Agent).filter(Agent.status == "active").all()
    since = _since(window)
    field, controls = board_size(db, since), control_ids(db)
    out = []
    for a in agents:
        q = _resolved_query(db, a.id, horizon, since)
        if pair:
            q = q.filter(Signal.pair == pair)
        open_q = _pending_query(db, a.id, horizon)
        rec = _record(q.all(), _slices(db, a.id, since, horizon, pair), competitors=field,
                      pending=(open_q.filter(Signal.pair == pair) if pair else open_q).all())
        if rec["ranked"]:
            if a.id in controls:
                rec = _as_control(rec)
            elif a.id in closed_ids():
                rec = _as_closed(db, rec, closed_ids()[a.id])
            out.append({"agent_id": a.id, "name": a.name, **rec})
    out.sort(key=lambda r: r["score"], reverse=True)
    place = 0
    for r in out:     # a yardstick or a closed identity is listed where its score puts it, but takes no place in the ranking
        if r["verdict"] in ("control", "closed"):
            r["rank"] = None
        else:
            place += 1
            r["rank"] = place
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
    records: dict[tuple[str, str], dict] = {}  # one record query per author per horizon, not per signal
    for s in rows:
        a = db.get(Agent, s.agent_id)
        key = (s.agent_id, s.horizon)
        if key not in records:
            records[key] = agent_record(db, s.agent_id, s.horizon, "90d")
        rec = records[key]
        out.append({
            "signal_id": s.id, "agent_id": s.agent_id, "agent_name": a.name if a else "",
            "pair": s.pair, "horizon": s.horizon, "submitted_at": s.submitted_at.isoformat(),
            "resolve_at": s.resolve_at.isoformat(), "hash": s.hash,
            "kind": s.kind or "plain", "venue": s.venue, "market": s.market,   # never the side, the limit or the fill
            "agent_score": rec["score"], "agent_ranked": rec["ranked"], "agent_n": rec["n"],
            "agent_verdict": rec["verdict"], "read_cost": read_cost(rec["verdict"]),
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


def read_cost(verdict: str) -> float:
    return config.READ_COST_BY_VERDICT.get(verdict, config.READ_COST)


def unseal_or_empty(sig: Signal) -> dict:
    """A sealed body that cannot be opened (corrupt row, changed master key) must not take the whole
    listing down: that one call shows null direction and confidence, and is logged once per request."""
    try:
        return unseal(sig.agent_id, sig.body_cipher)
    except Exception as e:
        log.warning("cannot unseal signal %s: %s", sig.id, type(e).__name__)
        return {}


def read_signal(db: Session, reader: Agent, signal_id: str) -> dict:
    sig = db.get(Signal, signal_id)
    if not sig:
        raise HTTPException(404, "no such signal")
    own = sig.agent_id == reader.id
    if not own:
        require_in_good_standing(reader)
        if sig.resolved_at is None and db.get(Agent, sig.agent_id).status != "active":
            raise HTTPException(404, "no such signal")   # a delisted author's open calls are hidden everywhere
    if not own:
        with _credit_lock:
            already = db.get(Read, (reader.id, signal_id)) is not None
            if not already:
                cost, verdict = 0.0, None
                if sig.resolved_at is None:  # open signals cost credits; resolved ones are public record
                    verdict = agent_record(db, sig.agent_id, sig.horizon, "90d")["verdict"]
                    cost = read_cost(verdict)
                    bal = balance(db, reader.id)
                    if bal < cost:
                        raise HTTPException(402, detail={"message": "insufficient credits", "balance": bal, "cost": cost})
                    db.add(CreditEntry(agent_id=reader.id, amount=-cost, reason="read", signal_id=sig.id))
                db.add(Read(agent_id=reader.id, signal_id=sig.id))
                db.add(AuditEvent(agent_id=reader.id, event="read", detail=sig.id))
                db.commit()
                if cost:
                    event("read", agent=reader.name or reader.id, author=_name(db, sig.agent_id), pair=sig.pair,
                          horizon=sig.horizon, verdict=verdict, cost=cost, left=balance(db, reader.id))
    body = unseal_or_empty(sig)
    return {
        "signal_id": sig.id, "agent_id": sig.agent_id, "pair": sig.pair, "horizon": sig.horizon,
        "direction": _direction(body), "confidence": body.get("confidence"),
        "submitted_at": sig.submitted_at.isoformat(), "resolve_at": sig.resolve_at.isoformat(),
        "price_at_submit": sig.price_at_submit, "start_at": sig.start_at.isoformat() if sig.start_at else None,
        "hash": sig.hash, "hash_version": sig.hash_version,
        "result": sig.result, "price_at_resolve": sig.price_at_resolve, "brier": sig.brier, "net_return": sig.net_return,
        # Once a call has closed it is public record, so its salt is too: with it anyone can recompute
        # the call's hash and prove its direction and confidence were not changed after the fact.
        "salt": body.get("salt") if sig.resolved_at is not None else None,
        **_priced_view(sig, body),
    }


def _direction(body: dict) -> str | None:
    """long | short. A priced call's side says it: these markets ask "will it finish higher?"."""
    return body.get("direction") or {"yes": "long", "no": "short"}.get(body.get("side"))


def _priced_view(sig: Signal, body: dict) -> dict:
    """The priced part of a call, for someone entitled to its body (its author, or a reader who paid)."""
    if sig.kind != "priced":
        return {"kind": "plain"}
    return {"kind": "priced", "venue": sig.venue, "market": sig.market, "side": body.get("side"),
            "limit": body.get("limit"), "stated": body.get("confidence"), "fill": fill_record(sig, body) if sig.fill else None,
            "venue_result": sig.venue_result, "leaf": sig.leaf}


def my_signals(db: Session, agent: Agent, limit: int = 200) -> list[dict]:
    rows = db.query(Signal).filter_by(agent_id=agent.id).order_by(Signal.submitted_at.desc()).limit(limit).all()
    out = []
    for s in rows:
        body = unseal_or_empty(s)
        out.append({
            "signal_id": s.id, "pair": s.pair, "horizon": s.horizon, "direction": _direction(body),
            "confidence": body.get("confidence"), "submitted_at": s.submitted_at.isoformat(),
            "resolve_at": s.resolve_at.isoformat(), "price_at_submit": s.price_at_submit,
            "start_at": s.start_at.isoformat() if s.start_at else None, "hash": s.hash, "hash_version": s.hash_version,
            "result": s.result, "price_at_resolve": s.price_at_resolve, "brier": s.brier,
            "net_return": s.net_return, "credit": s.credit, "scoring_version": s.scoring_version,
            **_priced_view(s, body),
        })
    return out


# ---------- daily log ----------

def leaf(s: Signal) -> str:
    """The day's root commits to this per call: the receipt hash, plus the start the room gave it (a
    plain call) or what the room saw at the venue (a priced call, stored once its fill is decided)."""
    if s.kind == "priced":
        return s.leaf or s.hash
    return leaf_hash(s.hash, s.start_at.isoformat(), s.price_at_submit) if s.start_at else s.hash


def day_rows(db: Session, day: dt.date) -> list[Signal]:
    start = dt.datetime(day.year, day.month, day.day)
    end = start + dt.timedelta(days=1)
    return db.query(Signal).filter(Signal.submitted_at >= start, Signal.submitted_at < end).order_by(Signal.submitted_at).all()


def day_ready_to_anchor(db: Session, day: dt.date) -> bool:
    """A day's root is final once every call posted that day has its start price, or for a priced call
    its fill decided (or is void)."""
    return not any((s.fill is None if s.kind == "priced" else s.price_at_submit is None) and s.resolved_at is None
                   for s in day_rows(db, day))


def build_daily_log(db: Session, day: dt.date) -> DailyLog:
    rows = day_rows(db, day)
    root = merkle_root([leaf(s) for s in rows])
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
    rows = day_rows(db, day)
    log = db.get(DailyLog, day.isoformat())
    return {
        "day": day.isoformat(),
        "merkle_root": merkle_root([leaf(s) for s in rows]),
        "anchored_root": log.merkle_root if log and log.anchored_tx else None,
        "anchored_tx": log.anchored_tx if log else None,
        "signals": [{"signal_id": s.id, "agent_id": s.agent_id, "pair": s.pair, "horizon": s.horizon,
                     "submitted_at": s.submitted_at.isoformat(), "resolve_at": s.resolve_at.isoformat(),
                     "resolved": s.resolved_at is not None, "hash": s.hash, "leaf": leaf(s), "hash_version": s.hash_version,
                     "start_at": s.start_at.isoformat() if s.start_at and s.kind is None else None,
                     "price_at_submit": s.price_at_submit, "kind": s.kind or "plain", "market": s.market}
                    for s in rows],
    }

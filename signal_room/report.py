"""Plain-English daily report, straight from the database. `./start.sh report [YYYY-MM-DD]`.

With no date: the last 24 hours. With a date: that UTC day. Numbers come from the same scoring code the
leaderboard uses, so nothing here is estimated. Safe to run while the room is up.
"""
from __future__ import annotations

import datetime as dt
import sys

from sqlalchemy import func

from . import config, service
from .db import Agent, AuditEvent, CreditEntry, DailyLog, ReferencePrice, SessionLocal, Signal, utcnow
from .feed import NAMES
from .status import VERDICT_WORDS, pct

BAR = 20   # characters for 0–100%; the middle mark is a coin flip


def bar(rate: float) -> str:
    """'██████████│███·······' : filled share is the right rate, │ sits at 50%."""
    filled = round(rate * BAR)
    cells = ["█" if i < filled else "·" for i in range(BAR)]
    return "".join(cells[: BAR // 2]) + "│" + "".join(cells[BAR // 2:])


def verdict_before(db, agent: Agent, start: dt.datetime) -> str | None:
    """The 90-day verdict as it stood when the report window opened."""
    rows = (db.query(Signal)
            .filter(Signal.agent_id == agent.id, Signal.result.in_(("right", "wrong", "flat")),
                    Signal.resolved_at < start, Signal.resolved_at >= start - dt.timedelta(days=90))
            .order_by(Signal.resolved_at).all())
    if not rows:
        return None
    return agent.status if agent.status != "active" else service._record(rows, 1, start)["verdict"]


def bot_lines(db, agent: Agent, start: dt.datetime, end: dt.datetime) -> list[str] | None:
    rows = (db.query(Signal)
            .filter(Signal.agent_id == agent.id, Signal.resolved_at >= start, Signal.resolved_at < end)
            .all())
    # Paid reads only: an open call bought with credits. Reading a closed call is free (the verify tool does it).
    reads = db.query(CreditEntry).filter(CreditEntry.agent_id == agent.id, CreditEntry.reason == "read",
                                         CreditEntry.created_at >= start, CreditEntry.created_at < end).count()
    if not rows and not reads:
        return None
    name = NAMES.get(agent.name or "", agent.name or agent.id)
    right = sum(s.result == "right" for s in rows)
    wrong = sum(s.result == "wrong" for s in rows)
    flat = sum(s.result == "flat" for s in rows)
    void = sum(s.result == "void" for s in rows)
    out = [name]
    if right + wrong:
        rate = right / (right + wrong)
        out.append(f"  {bar(rate)}  right {pct(rate)} ({right} of {right + wrong})"
                   + (f", {flat} flat" if flat else "") + (f", {void} voided" if void else ""))
    elif rows:
        out.append(f"  nothing right or wrong: {flat} flat, {void} voided")
    if reads:
        out.append(f"  read {reads} call{'s' if reads != 1 else ''} from other bots")
    now = service.public_agent(db, agent, None, "90d")
    before = verdict_before(db, agent, start)
    word = VERDICT_WORDS[now["verdict"]]
    if before and before != now["verdict"]:
        out.append(f"  verdict moved: {VERDICT_WORDS[before]} → {word}")
    else:
        out.append(f"  verdict: {word} (unchanged)")
    return out


def report(db, day: dt.date | None = None, now: dt.datetime | None = None) -> str:
    if day is None:
        end = now or utcnow()
        start = end - dt.timedelta(hours=24)
        title = f"Signal Room, last 24 hours (to {dt.datetime.now().strftime('%a %b %d %H:%M')} local)"
    else:
        start = dt.datetime.combine(day, dt.time())
        end = start + dt.timedelta(days=1)
        title = f"Signal Room, {day.isoformat()} (UTC day)"
    lines = [title, ""]

    scored = db.query(Signal).filter(Signal.resolved_at >= start, Signal.resolved_at < end)
    right = scored.filter(Signal.result == "right").count()
    wrong = scored.filter(Signal.result == "wrong").count()
    posted = db.query(Signal).filter(Signal.submitted_at >= start, Signal.submitted_at < end).count()
    lines.append(f"{posted} new calls, {scored.count()} closed: {right} right, {wrong} wrong"
                 + (f" ({pct(right / (right + wrong))} right; a coin flip is 50%)" if right + wrong else ""))
    lines.append("")

    lines.append("Bots (bar = share right, │ = coin flip):")
    shown = 0
    for a in db.query(Agent).order_by(Agent.created_at).all():
        block = bot_lines(db, a, start, end)
        if block:
            lines.extend(block)
            shown += 1
    if not shown:
        lines.append("  no bot closed a call in this window")
    lines.append("")

    gaps = db.query(AuditEvent).filter(AuditEvent.event == "tick_gap", AuditEvent.created_at >= start,
                                       AuditEvent.created_at < end).count()
    rebuilt = db.query(ReferencePrice).filter(ReferencePrice.source == "backfill", ReferencePrice.sampled_at >= start,
                                              ReferencePrice.sampled_at < end).count()
    lines.append("Room health:")
    lines.append(f"  price sampler: {gaps} gap{'s' if gaps != 1 else ''} (laptop asleep or room stopped)"
                 + (f", {rebuilt} minutes rebuilt from exchange candles" if rebuilt else ""))
    last = (db.query(DailyLog).filter(DailyLog.anchored_tx.isnot(None))
            .order_by(DailyLog.day.desc()).first())
    first = db.query(func.min(Signal.submitted_at)).scalar()
    done = {d for (d,) in db.query(DailyLog.day).filter(DailyLog.anchored_tx.isnot(None))}
    today = utcnow().date()
    waiting = 0 if first is None else sum(
        (first.date() + dt.timedelta(days=i)).isoformat() not in done for i in range((today - first.date()).days))
    if last:
        lines.append(f"  on-chain proof: anchored through {last.day} ({last.signal_count} calls that day)"
                     + (f"; {waiting} closed day{'s' if waiting != 1 else ''} still waiting" if waiting else "; nothing waiting"))
    else:
        lines.append("  on-chain proof: nothing anchored yet")
    last_price = db.query(func.max(ReferencePrice.sampled_at)).scalar()
    if last_price is None or (utcnow() - last_price).total_seconds() > config.PRICE_MAX_AGE_SECONDS:
        lines.append("  WARNING: the price sampler is not running right now. Start the room with: room")
    lines.append("")
    lines.append("One day is a small sample: a coin-flipper lands anywhere from 35% to 65% on a day of 40 calls."
                 " The verdicts are what count.")
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    day = None
    if argv:
        try:
            day = dt.date.fromisoformat(argv[0])
        except ValueError:
            print("usage: ./start.sh report [YYYY-MM-DD]")
            sys.exit(1)
    db = SessionLocal()
    try:
        print(report(db, day))
    finally:
        db.close()


if __name__ == "__main__":
    main(sys.argv[1:])

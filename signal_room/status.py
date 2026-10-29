"""Plain-English status of the room, straight from the database. `./start.sh status`.

Reads the same tables the API reads and runs the same scoring code, so the numbers here are the
numbers the leaderboard shows. Nothing is cached or estimated. Safe to run while the server is up.
"""
from __future__ import annotations

import datetime as dt
import sys
from collections import defaultdict

from sqlalchemy import func

from . import config, scoring, service
from .db import Agent, AuditEvent, ReferencePrice, SessionLocal, Signal, utcnow


def ago(then: dt.datetime | None, now: dt.datetime) -> str:
    if then is None:
        return "never"
    s = int((now - then).total_seconds())
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s / 3600:.1f} h ago"
    return f"{s // 86400} days ago"


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def signed(x: float, places: int = 2) -> str:
    return f"{x:+.{places}f}"


def luck(p: float) -> str:
    """'1 in 50' reads better than 0.02 for a non-statistician."""
    if p >= 0.5:
        return "about even odds"
    if p <= 0:
        return "under 1 in a million"
    return f"about 1 in {round(1 / p):,}"


EVIDENCE_WORDS = {
    "insufficient": "too few signals to say anything",
    "none": "could easily be luck",
    "suggestive": "probably not luck, but not proven",
    "strong": "very unlikely to be luck",
}


def agent_lines(db, a: Agent, now: dt.datetime) -> list[str]:
    rows = db.query(Signal).filter(Signal.agent_id == a.id).all()
    open_ = [s for s in rows if s.resolved_at is None]
    void = [s for s in rows if s.result == "void"]
    rec = service.agent_record(db, a.id, None, "90d")
    status = a.status if a.status != "active" else "active"
    head = f"{a.name or a.id}  ({status}"
    if a.delist_count:
        head += f", delisted {a.delist_count}x in 12 months"
    head += ")"
    out = [head]
    out.append(f"    scored {rec['n']}, flat {rec['flats']}, open {len(open_)}, voided {len(void)}, credits {service.balance(db, a.id):.1f}")
    old_flats = sum(1 for s in rows if s.result == "flat" and s.brier is not None)
    if old_flats:
        out.append(f"    note: {old_flats} flats scored under the old rules (before rule set {scoring.SCORING_VERSION}) still count toward skill")
    if rec["n"] == 0:
        out.append("    nothing scored yet")
        return out
    out.append(f"    right {pct(rec['hit_rate'])} of the time (95% range {pct(rec['hit_rate_low'])}–{pct(rec['hit_rate_high'])});"
               f" a coin-flipper does this well {luck(rec['p_luck'])}")
    out.append(f"    skill {signed(rec['bss'])} (range {signed(rec['bss_low'])} to {signed(rec['bss_high'])}): {EVIDENCE_WORDS[rec['evidence']]}")
    if rec["ranked"]:
        out.append(f"    leaderboard score {signed(rec['score'], 3)}")
    else:
        out.append(f"    needs {config.MIN_RESOLVED_FOR_RANK - rec['n']} more scored signals to be ranked")
    out.append(f"    if traded 1:1 at {config.ROUND_TRIP_COST * 100:.2f}% cost per trade: {signed(rec['cum_return'] * 100)}% total,"
               f" worst dip {rec['max_drawdown'] * 100:.1f}%")

    by_pair: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_hz: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    day_ago = now - dt.timedelta(days=1)
    recent = [0, 0]
    for s in rows:
        if s.result not in ("right", "wrong"):
            continue
        by_pair[s.pair][1] += 1
        by_hz[s.horizon][1] += 1
        if s.result == "right":
            by_pair[s.pair][0] += 1
            by_hz[s.horizon][0] += 1
        if s.resolved_at and s.resolved_at >= day_ago:
            recent[1] += 1
            recent[0] += s.result == "right"
    out.append("    by pair: " + ", ".join(f"{p} {r}/{n}" for p, (r, n) in sorted(by_pair.items())))
    if len(by_hz) > 1:
        out.append("    by horizon: " + ", ".join(f"{h} {r}/{n}" for h, (r, n) in sorted(by_hz.items())))
    out.append(f"    last 24h: {recent[0]}/{recent[1]} right" if recent[1] else "    last 24h: nothing scored")
    return out


def report(db) -> str:
    now = utcnow()
    lines = [f"Signal Room status, {dt.datetime.now().strftime('%a %b %d %H:%M')} local"]

    last_price = db.query(func.max(ReferencePrice.sampled_at)).scalar()
    day_start = now - dt.timedelta(days=1)
    gaps = db.query(AuditEvent).filter(AuditEvent.event == "tick_gap", AuditEvent.created_at >= day_start).all()
    backfilled = db.query(ReferencePrice).filter(ReferencePrice.source == "backfill", ReferencePrice.sampled_at >= day_start).count()
    lines.append(f"prices: last sample {ago(last_price, now)}; gaps in last 24h: {len(gaps)}"
                 + (f" ({', '.join(g.detail for g in gaps)})" if gaps else "")
                 + (f"; {backfilled} minutes rebuilt from exchange candles" if backfilled else ""))
    if last_price is None or (now - last_price).total_seconds() > config.PRICE_MAX_AGE_SECONDS:
        lines.append("WARNING: the price sampler is not running. Start the server with ./start.sh")

    total = db.query(Signal).count()
    open_ = db.query(Signal).filter(Signal.resolved_at.is_(None)).count()
    lines.append(f"signals: {total} total, {open_} open")
    lines.append("")

    board = service.leaderboard(db, None, None, "90d")
    lines.append("Leaderboard (90-day window, ranked agents only):")
    if board:
        for r in board:
            lines.append(f"  {r['rank']}. {r['name']}  score {signed(r['score'], 3)}  ({r['n']} scored, {pct(r['hit_rate'])} right, {EVIDENCE_WORDS[r['evidence']]})")
    else:
        lines.append(f"  nobody ranked yet (needs {config.MIN_RESOLVED_FOR_RANK} scored signals)")
    lines.append("")

    agents = db.query(Agent).order_by(Agent.created_at).all()
    lines.append("Agents:")
    for a in agents:
        if db.query(Signal).filter(Signal.agent_id == a.id).count() == 0:
            continue
        lines.extend(agent_lines(db, a, now))
        lines.append("")
    return "\n".join(lines).rstrip()


def main() -> None:
    db = SessionLocal()
    try:
        print(report(db))
    finally:
        db.close()


if __name__ == "__main__":
    main()

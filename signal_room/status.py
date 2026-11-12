"""Plain-English status of the room, straight from the database. `./start.sh status`.

A record of plain calls is read against a coin flip, as it always was. A record that holds priced
calls is read against its bar: what those calls cost, fees included.

Reads the same tables the API reads and runs the same scoring code, so the numbers here are the
numbers the leaderboard shows. Nothing is cached or estimated. Safe to run while the server is up.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from sqlalchemy import func

from . import config, service
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


VERDICT_WORDS = {
    "unproven": "Unproven",
    "luck": "Luck",
    "probably_not_luck": "Probably not luck",
    "proven": "Proven",
    "worse_than_chance": "Worse than chance",
    "delisted": "Delisted",
    "retired": "Retired",
    "control": "Control",
}


def points(x: float) -> str:
    return f"{x * 100:+.0f}"


def headline(rec: dict) -> str:
    """'Luck · right 60% (43–74%) · skill +8'. A record with priced calls is read against what they
    cost: 'Luck · right 74% against a bar of 68% (edge +6, range -2 to +13) · skill +1'."""
    if rec.get("priced"):
        return (f"{VERDICT_WORDS[rec['verdict']]} · right {pct(rec['hit_rate'])} against a bar of {pct(rec['bar'])}"
                f" (edge {points(rec['edge'])}, range {points(rec['edge_low'])} to {points(rec['edge_high'])})"
                f" · skill {rec['skill_points']:+d}")
    return (f"{VERDICT_WORDS[rec['verdict']]} · right {pct(rec['hit_rate'])} ({pct(rec['hit_rate_low'])}–{pct(rec['hit_rate_high'])})"
            f" · skill {rec['skill_points']:+d}")


def progress(rec: dict) -> str:
    nxt = rec.get("next_verdict")
    if nxt is None:
        return ""
    if rec["tries_to_next"] is None:
        return f"next step ({VERDICT_WORDS[nxt]}): not heading there at this record"
    d = rec.get("days_to_next")
    days = "" if d is None else (", over a year at this pace" if d > 365 else f", about {d:g} days at this pace")
    return (f"next step ({VERDICT_WORDS[nxt]}): about {rec['tries_to_next']:,} more tries{days},"
            " if the next stretch looks like the record so far")


def agent_lines(db, a: Agent, now: dt.datetime) -> list[str]:
    rows = db.query(Signal).filter(Signal.agent_id == a.id).all()
    open_ = [s for s in rows if s.resolved_at is None]
    void = [s for s in rows if s.result == "void"]
    rec = service.public_agent(db, a, None, "90d")
    head = f"{a.name or a.id}  ({a.status}"
    if a.delist_count:
        head += f", delisted {a.delist_count}x in 12 months"
    out = [head + ")"]
    if rec["n"] == 0:
        out.append(f"    nothing scored yet; open {len(open_)}")
        return out
    out.append("    " + headline(rec))
    nobody = "a bot with no edge over its prices" if rec.get("priced") else "a coin-flipper"
    if rec["verdict"] == "worse_than_chance":
        odds = f"{nobody} does this badly {luck(rec['p_worse'])}"
    else:
        odds = f"{nobody} does this well {luck(rec['p_luck'])}"
    field = f"; bar raised {rec['competitors']}x for the {rec['competitors']} ranked bots a reader chooses among" if rec["competitors"] > 1 else ""
    out.append(f"    {odds}, over {rec['tries']} separate tries (calls that share market time count once){field}")
    if rec["verdict"] == "control":
        out.append("    a yardstick: its numbers are there to compare against; it gets no verdict and does not count toward the board")
    if not rec["ranked"]:
        out.append(f"    needs {config.MIN_RESOLVED_FOR_RANK - rec['n']} more scored signals to be ranked")
    if progress(rec):
        out.append("    " + progress(rec))
    unfilled = sum(1 for s in rows if s.result == "unfilled")
    out.append(f"    scored {rec['n']}, flat {rec['flats']}, open {len(open_)}, voided {len(void)}"
               + (f", not filled {unfilled}" if unfilled else "") + f", credits {service.balance(db, a.id):.1f}")
    if rec["priced"] < rec["n"]:
        out.append(f"    calls vs money: ranked on calls only. If traded 1:1 at {config.ROUND_TRIP_COST * 100:.2f}% cost per trade:"
                   f" {signed(rec['cum_return'] * 100)}% (worst dip {rec['max_drawdown'] * 100:.1f}%);"
                   f" always-long over the same windows: {signed(rec['always_long_return'] * 100)}%")
    if rec["priced"]:
        out.append(f"    priced calls: {rec['priced']} bought at the price the room read; average profit"
                   f" {rec['profit_per_contract'] * 100:+.1f}¢ a contract after fees")
    judged = [c for c in rec["calibration"] if c["judged"] != "too few"]
    if judged:
        out.append("    calibration: " + "; ".join(
            f"says {c['band']}, right {pct(c['actual'])} ({pct(c['actual_low'])}–{pct(c['actual_high'])}) {c['judged']}"
            for c in judged))
    else:
        out.append("    calibration: too few tries in any confidence band to judge yet")

    by_pair: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_hz: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    day_ago = now - dt.timedelta(days=1)
    since = service._since("90d")
    recent = [0, 0]
    for s in rows:
        if s.result not in ("right", "wrong") or s.resolved_at < since:   # same window as the headline
            continue
        by_pair[s.pair][1] += 1
        by_hz[s.horizon][1] += 1
        if s.result == "right":
            by_pair[s.pair][0] += 1
            by_hz[s.horizon][0] += 1
        if s.resolved_at and s.resolved_at >= day_ago:
            recent[1] += 1
            recent[0] += s.result == "right"
    out.append("    slices over the same 90 days, exploratory (not verdicts; each one faces a higher bar):")
    out.append("      by coin: " + ", ".join(f"{p} {r}/{n}" for p, (r, n) in sorted(by_pair.items())))
    if len(by_hz) > 1:
        out.append("      by window: " + ", ".join(f"{h} {r}/{n}" for h, (r, n) in sorted(by_hz.items())))
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
            lines.append(f"  {r['rank']}. {r['name']}  {headline(r)}  ({r['n']} scored, {r['tries']} tries)")
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

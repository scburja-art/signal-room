"""The live feed in `./start.sh all`, written for a person, not a programmer.

Reads every log line, hides routine noise, and turns what is left into one plain sentence per thing
that happened: a call posted (sealed), a call scored (revealed, right or wrong), a paid read, a bot
delisted, a problem. Every bot has its own color. A short scoreboard prints at start and after
scoring, at most every half hour.

    tail -F logs/*.log | python -m signal_room.feed
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time

RESET, BOLD = "\033[0m", "\033[1m"
GREEN, RED, YELLOW, GRAY, WHITE = "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[37m"

FAMILY_COLORS = {"kakalshi": "\033[93m", "momentum": "\033[94m", "meanrev": "\033[95m", "reader": "\033[96m"}
NAMES = {
    "momentum-control": "Momentum",
    "meanrev-control": "Mean-rev",
    "reader-control": "Reader",
    "kakalshi-crypto-first-minute": "Kakalshi CFM",
    "kakalshi-crypto-imbalance": "Kakalshi OBI",
}
LOGGER_NAMES = {"agents.momentum": "Momentum", "agents.meanrev": "Mean-rev", "agents.reader": "Reader",
                "agents.kakalshi": "Kakalshi", "signal_room": "Room"}
HORIZONS = {"15m": "15 min", "1h": "1 hour", "4h": "4 hours", "24h": "24 hours"}
VERDICTS = {"unproven": "Unproven", "luck": "Luck", "probably_not_luck": "Probably not luck", "proven": "Proven",
            "worse_than_chance": "Worse than chance", "delisted": "Delisted", "retired": "Retired"}

LINE = re.compile(r"^\d{4}-\d\d-\d\d (\d\d):(\d\d):\d\d,\d+ (\w+) ([\w.]+): (.*)$")
# Lines the feed never shows: retries, the agents' own copies of what the server reports, routine requests.
HIDE = re.compile(
    r"still pending|already pending|will retry|HTTP/1\.1\" [234]\d\d |'resolved': \d+\}|^==> |^\s*$"
    r"|^(BTC|ETH|SOL|XRP) (long|short) @|^bet \S+ \[.*\] -> |^read \S+ \S+ \S+ by |nothing affordable to read"
    r"|^(Started|Waiting for|Application|Finished|Shutting down)"
)


def display(name: str) -> str:
    return NAMES.get(name, name if len(name) <= 14 else name[:13] + "…")


def color_of(name: str) -> str:
    low = name.lower()
    for family, code in FAMILY_COLORS.items():
        if family in low or (family == "meanrev" and "mean-rev" in low):
            return code
    return WHITE


def money(p: float) -> str:
    if p >= 10000:
        return f"${p:,.0f}"
    if p >= 10:
        return f"${p:,.2f}"
    return f"${p:.4f}"


def clock(hour: int, minute: int) -> str:
    return dt.time(hour, minute).strftime("%I:%M %p").lstrip("0")


def local(iso_utc: str) -> str:
    """A UTC timestamp from the server, as a local clock time."""
    t = dt.datetime.fromisoformat(iso_utc).replace(tzinfo=dt.timezone.utc).astimezone()
    return t.strftime("%I:%M %p").lstrip("0")


class Painter:
    def __init__(self, color: bool):
        self.color = color

    def __call__(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.color else text

    def row(self, when: str, who: str, badge: str, badge_code: str, details: str) -> str:
        name = self(color_of(who) + BOLD, f"{display(who):<13}")
        return f"{self(GRAY, f'{when:>8}')}  {name} {self(badge_code, f'{badge:<10}')} {details}"


def render_event(e: dict, when: str, p: Painter) -> str | None:
    kind, who = e.get("kind"), e.get("agent", "?")
    where = f"{e.get('pair', '')} · {HORIZONS.get(e.get('horizon'), e.get('horizon', ''))}"
    if kind == "posted":
        return p.row(when, who, "NEW CALL", WHITE, f"{where} · sealed until {local(e['closes'])}")
    if kind == "scored":
        result = e.get("result")
        if result == "void":
            return p.row(when, who, "– VOID", GRAY, f"{where} · no price could be found, not scored")
        said = f"said {'UP' if e['direction'] == 'long' else 'DOWN'}, {e['confidence'] * 100:.0f}% sure"
        moved = f"{money(e['start'])} → {money(e['end'])}"
        if result == "flat":
            return p.row(when, who, "– FLAT", GRAY, f"{where} · {said} · {moved} · barely moved, not scored")
        badge, code = ("✔ RIGHT", GREEN + BOLD) if result == "right" else ("✘ WRONG", RED + BOLD)
        credit = p(GREEN if e["credit"] >= 0 else RED, f"{e['credit']:+.2f} credits")
        return p.row(when, who, badge, code, f"{where} · {said} · {moved} · {credit}")
    if kind == "read":
        verdict = VERDICTS.get(e.get("verdict"), e.get("verdict") or "")
        cost = f"{e['cost']:g} credit{'s' if e['cost'] != 1 else ''}"
        return p.row(when, who, "READ", WHITE,
                     f"paid {cost} for {display(e['author'])}'s {where} call ({verdict}) · {e['left']:g} left")
    if kind == "delisted":
        return p.row(when, who, "DELISTED", RED + BOLD, "skill fell below the line over its last 90 calls; it can post its way back")
    if kind == "retired":
        return p.row(when, who, "RETIRED", RED + BOLD, "delisted three times in a year; this wallet is done")
    if kind == "reinstated":
        return p.row(when, who, "BACK", GREEN + BOLD, "earned its way back with 30 fresh calls above a coin flip")
    return None


def render(line: str, color: bool = True) -> str | None:
    """One log line in, one readable line out, or None to hide it."""
    p = Painter(color)
    line = line.rstrip("\n")
    m = LINE.match(line)
    if not m:
        if HIDE.search(line):
            return None
        if re.search(r"Traceback|Error|Exception", line):
            return p(RED, f"  {line}")
        return p(GRAY, f"  {line}")
    hour, minute, level, logger, msg = m.groups()
    when = clock(int(hour), int(minute))
    if msg.startswith("EVENT "):
        try:
            return render_event(json.loads(msg[6:]), when, p)
        except (ValueError, KeyError, TypeError):
            return None
    who = next((v for k, v in LOGGER_NAMES.items() if logger.startswith(k)), logger.split(".")[-1])
    gap = re.match(r"gap of (\d+)s since last tick", msg)
    if gap:
        mins = int(gap.group(1)) // 60
        span = f"{mins} min" if mins < 90 else f"{mins / 60:.0f} hours"
        return p.row(when, "Room", "⚠ ASLEEP", YELLOW, f"the laptop was asleep or off for about {span}; missed prices get rebuilt from the exchanges")
    anchored = re.match(r"anchored (\d{4}-\d\d-\d\d) root (\w+)… in (0x\w+)", msg)
    if anchored:
        day = dt.date.fromisoformat(anchored.group(1)).strftime("%b %d").replace(" 0", " ")
        return p.row(when, "Room", "⛓ ANCHORED", GREEN, f"{day}'s fingerprint of every call is now on the public chain (tx {anchored.group(3)[:10]}…)")
    failed = re.match(r"anchor failed for (\S+): (.*)", msg)
    if failed:
        return p.row(when, "Room", "⚠ ANCHOR", YELLOW, f"couldn't write {failed.group(1)}'s fingerprint yet ({failed.group(2)[:60]}); retrying next minute")
    if HIDE.search(msg):
        return None
    if "running as" in msg or "adapter watching" in msg:
        return p.row(when, who, "ONLINE", GRAY, p(GRAY, "watching Kakalshi's paper bets" if "watching" in msg else "connected to the room"))
    candle = re.match(r"(BTC|ETH|SOL|XRP): candle fetch failed: (.*)", msg)
    if candle:
        return p.row(when, who, "⚠ SKIPPED", YELLOW, f"couldn't get {candle.group(1)}'s last hour from Coinbase ({candle.group(2)}); no {candle.group(1)} call this round")
    if level == "WARNING":
        return p.row(when, who, "⚠ WARNING", YELLOW, msg)
    if level in ("ERROR", "CRITICAL"):
        return p.row(when, who, "✘ ERROR", RED + BOLD, msg)
    return p.row(when, who, "", GRAY, p(GRAY, msg))


def versus_coin_flip(points: int) -> tuple[str, str]:
    """Skill as a direction a glance can read: (text, color)."""
    if abs(points) <= 1:
        return "≈ even", GRAY
    if points > 0:
        return f"▲ {points} better", GREEN
    return f"▼ {-points} worse", RED


def days_text(days: float | None) -> str:
    if days is None:
        return "—"
    if days > 365:
        return "over a year"
    d = max(1, round(days))
    return f"~{d} day{'s' if d != 1 else ''}"


def scoreboard(color: bool = True) -> list[str]:
    """Every bot's standing in one short block, from the database. One header row labels the
    columns so each line holds only values."""
    from . import service
    from .db import Agent, Read, SessionLocal, Signal

    p = Painter(color)
    db = SessionLocal()
    try:
        bots, readers = [], []
        for a in db.query(Agent).order_by(Agent.created_at).all():
            if db.query(Signal).filter(Signal.agent_id == a.id).count():
                r = service.public_agent(db, a, None, "90d")
                bots.append((a, r))
                continue
            reads = db.query(Signal.result).join(Read, Read.signal_id == Signal.id).filter(
                Read.agent_id == a.id, Signal.agent_id != a.id).all()
            if reads:
                done = [x[0] for x in reads if x[0] in ("right", "wrong")]
                readers.append((a, len(reads), done.count("right"), len(done), service.balance(db, a.id)))

        vw = max([len("Verdict")] + [len(VERDICTS.get(r["verdict"], r["verdict"])) for _, r in bots])
        title = f" Scoreboard · {dt.datetime.now().strftime('%I:%M %p').lstrip('0')} "
        out = ["", p(BOLD, f"──{title}" + "─" * max(4, 70 - len(title)))]
        out.append(p(GRAY, f"  {'Bot':<13} {'Verdict':<{vw}}  {'Right':>5}   {'vs. coin flip':<13}  {'Calls':>5}   Next step"))
        for a, r in bots:
            name = p(color_of(a.name) + BOLD, f"{display(a.name):<13}")
            verdict = VERDICTS.get(r["verdict"], r["verdict"])
            vcode = {"proven": GREEN, "probably_not_luck": GREEN, "worse_than_chance": RED,
                     "delisted": RED, "retired": RED}.get(r["verdict"], WHITE)
            if r["n"] == 0:
                skill, scode, right, nxt = "—", GRAY, "—", "—"
            else:
                skill, scode = versus_coin_flip(r["skill_points"])
                right = f"{r['hit_rate'] * 100:.0f}%"
                nxt = days_text(r.get("days_to_next")) if r["verdict"] != "unproven" else "—"
            out.append(f"  {name} {p(vcode, f'{verdict:<{vw}}')}  {right:>5}   {p(scode, f'{skill:<13}')}  {r['n']:>5}   "
                       + (p(GRAY, nxt) if nxt == "—" else nxt))
        if readers:
            out.append(p(GRAY, "  ─ ─ ─"))
            for a, n, right, closed, credits in readers:
                name = p(color_of(a.name) + BOLD, f"{display(a.name):<13}")
                out.append(f"  {name} {n} paid read{'s' if n != 1 else ''} · {right} of {closed} right · {credits:g} credit{'' if credits == 1 else 's'} left")
        out.append(p(GRAY, "  Ladder: Unproven → Luck → Probably not luck → Proven"))
        out.append(p(BOLD, "─" * 72))
        out.append("")
        return out
    finally:
        db.close()


BOARD_EVERY_SECONDS = 1800
ROUND_DONE = re.compile(r"tick \{.*'resolved': [1-9]")


def main() -> None:
    color = sys.stdout.isatty()
    last_board = 0.0

    def board():
        nonlocal last_board
        try:
            print("\n".join(scoreboard(color)), flush=True)
        except Exception as e:  # the feed must never die because the scoreboard could not be drawn
            print(Painter(color)(YELLOW, f"  (scoreboard unavailable: {e})"), flush=True)
        last_board = time.monotonic()

    board()
    for line in sys.stdin:
        out = render(line, color)
        if out is not None:
            print(out, flush=True)
        # The scheduler's tick line comes after a whole round of scoring: show the board then.
        if ROUND_DONE.search(line) and time.monotonic() - last_board >= BOARD_EVERY_SECONDS:
            board()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass

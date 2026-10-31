"""The live feed in `./start.sh all`: reads every log line, drops routine noise, shortens and colors
the rest. Each bot has its own color; scored calls show green for right, red for wrong.

    tail -F logs/*.log | python -m signal_room.feed
"""
from __future__ import annotations

import re
import sys

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
GREEN, RED, YELLOW, GRAY = "\033[32m", "\033[31m", "\033[33m", "\033[90m"

# Bot colors. Anything not listed (a new Kakalshi strategy, a stranger in a future log) gets white.
WHO = {
    "kakalshi": "\033[93m",          # bright yellow
    "momentum": "\033[94m",          # bright blue
    "meanrev": "\033[95m",           # bright magenta
    "mean-reversion": "\033[95m",
    "reader": "\033[96m",            # bright cyan
    "room": "\033[37m",              # the server itself
}

NOISE = re.compile(r"still pending|already pending|HTTP/1\.1\" [234]\d\d |'resolved': 0\}|^==> |^\s*$")
LINE = re.compile(r"^\d{4}-\d\d-\d\d (\d\d:\d\d:\d\d),\d+ (\w+) ([\w.]+): (.*)$")
SCORED = re.compile(r"^scored (\S+) (.*)= (right|wrong|flat|void)(.*)$")


def who_of(logger: str, author: str | None = None) -> str:
    """Short name for a log line's source: the scored call's author, or the logger that wrote it."""
    name = author or logger
    for key in ("kakalshi", "momentum", "meanrev", "mean-reversion", "reader"):
        if key in name:
            return "meanrev" if key == "mean-reversion" else key
    return "room" if logger.startswith("signal_room") or logger.startswith("uvicorn") else name.split(".")[-1]


def paint(who: str, text: str, color: bool) -> str:
    if not color:
        return text
    return f"{WHO.get(who, '')}{text}{RESET}"


def render(line: str, color: bool = True) -> str | None:
    """One log line in, one feed line out (or None to hide it)."""
    line = line.rstrip("\n")
    if NOISE.search(line):
        return None
    m = LINE.match(line)
    if not m:
        # Tracebacks and uvicorn's own lines have no timestamp: errors stay loud, the rest goes dim.
        if re.search(r"Traceback|Error|Exception|failed", line):
            return f"{RED}{line}{RESET}" if color else line
        return f"{GRAY}{line}{RESET}" if color else line
    clock, level, logger, msg = m.groups()
    s = SCORED.match(msg)
    if s:
        author, call, result, tail = s.groups()
        who = who_of(logger, author)
        mark = {"right": GREEN + BOLD, "wrong": RED + BOLD, "flat": GRAY, "void": GRAY}[result]
        body = f"{call}= {mark}{result.upper()}{RESET}{tail}" if color else f"{call}= {result.upper()}{tail}"
        tag = paint(who, f"{who:>9}", color)
        return f"{GRAY if color else ''}{clock}{RESET if color else ''} {tag} {'│' if color else '|'} scored {body}"
    who = who_of(logger)
    if msg.startswith("tick "):
        msg = msg.replace("tick ", "scoring tick ")
    if level in ("WARNING",):
        msg = f"{YELLOW}{msg}{RESET}" if color else msg
    elif level in ("ERROR", "CRITICAL"):
        msg = f"{RED}{BOLD}{msg}{RESET}" if color else msg
    tag = paint(who, f"{who:>9}", color)
    sep = "│" if color else "|"
    return f"{GRAY if color else ''}{clock}{RESET if color else ''} {tag} {sep} {msg}"


def main() -> None:
    color = sys.stdout.isatty()
    for line in sys.stdin:
        out = render(line, color)
        if out is not None:
            print(out, flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass

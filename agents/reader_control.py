"""Reader control: proves the read economy end to end.

Once an hour it reads one open call, choosing the author with the strongest verdict it can afford
(never itself) and keeping a small reserve. When a call it paid for closes, it records whether that
call was right, so the room learns whether reading was worth the credits. It never posts, so it never
earns: when its starter credits run out it stops reading. That is the design. Reading is earned.
"""
from __future__ import annotations

import json
import logging
import os
import time

from .client import SignalRoomClient, SignalRoomError

log = logging.getLogger("agents.reader")
RESERVE = 1.0
INTERVAL_SECONDS = 3600
PREFERENCE = ("proven", "probably_not_luck", "worse_than_chance", "luck", "unproven")


def load(path: str) -> dict:
    try:
        with open(path) as f:
            state = json.load(f)
        return {"read": state.get("read", {}), "done": state.get("done", {})}
    except (OSError, ValueError):
        return {"read": {}, "done": {}}


def save(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


def choose(listing: list[dict], me_id: str, already: set, credits: float) -> dict | None:
    """Best affordable unread call by someone else: strongest verdict first, then leaderboard score."""
    options = [s for s in listing
               if s["agent_id"] != me_id and s["signal_id"] not in already
               and s.get("read_cost", 1.0) <= credits - RESERVE]
    if not options:
        return None
    rank = {v: i for i, v in enumerate(PREFERENCE)}
    return min(options, key=lambda s: (rank.get(s.get("agent_verdict"), len(PREFERENCE)), -s.get("agent_score", 0.0)))


def settle(client, state: dict, now_iso: str) -> None:
    """Look up calls it paid for that have closed. Reading a resolved call is free."""
    for sid, info in state["read"].items():
        if sid in state["done"] or info["resolve_at"] > now_iso:
            continue
        body = client.read(sid)
        if body.get("result"):
            state["done"][sid] = body["result"]


def tally(state: dict) -> str:
    results = list(state["done"].values())
    return (f"paid reads {len(state['read'])}, closed {len(results)}: right {results.count('right')},"
            f" wrong {results.count('wrong')}, flat {results.count('flat')}")


def run_once(client, state: dict, now_iso: str) -> None:
    try:
        settle(client, state, now_iso)
        credits = client.me()["credits"]
        pick = choose(client.room(), client.agent_id, set(state["read"]), credits)
        if pick is None:
            log.info("nothing affordable to read (credits %.2f); %s", credits, tally(state))
            return
        body = client.read(pick["signal_id"])
        state["read"][pick["signal_id"]] = {"author": pick["agent_name"], "verdict": pick.get("agent_verdict"),
                                            "cost": pick.get("read_cost"), "pair": body["pair"],
                                            "horizon": body["horizon"], "resolve_at": body["resolve_at"]}
        log.info("read %s %s %s by %s (%s, cost %s); %s", body["pair"], body["horizon"], body["direction"],
                 pick["agent_name"], pick.get("agent_verdict"), pick.get("read_cost"), tally(state))
    except SignalRoomError as e:
        log.warning("%s", e)


def main(base_url: str, state_path: str) -> None:
    client = SignalRoomClient.load_or_register(base_url, state_path, name="reader-control")
    reads_path = state_path.replace(".json", "-reads.json")
    state = load(reads_path)
    log.info("reader control running as %s", client.agent_id)
    while True:
        try:
            run_once(client, state, time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()))
            save(reads_path, state)
        except Exception as e:   # server restart, network blip: the bot must outlive it
            log.warning("cycle failed (%s), retrying next hour", type(e).__name__)
        time.sleep(INTERVAL_SECONDS)

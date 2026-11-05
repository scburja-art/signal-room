"""Check the room's history yourself: `./start.sh verify 2026-11-03`.

For one closed day this checks three links, using its own math and only what any registered agent
can see, so it trusts nothing the room says:

  1. every closed call's hash, recomputed from what the call revealed when it closed (direction,
     confidence, price, time, salt), matches the hash the room published for it that day;
  2. the day's Merkle root, rebuilt from those hashes, matches the root the room says it anchored;
  3. that root is exactly what the public blockchain transaction carries.

If someone changed a call after the fact, link 1 breaks. If calls were added or removed, link 2
breaks. If the published record was rewritten after anchoring, link 3 breaks.

Reading a closed call is free. Requests are paced under the room's rate limit, so a full day takes a
few minutes; `--sample N` checks N random calls instead of all.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import sys
import time

import httpx

RPC_URL = os.getenv("ANCHOR_RPC_URL", "https://sepolia.base.org")
EXPLORER = os.getenv("ANCHOR_EXPLORER", "https://sepolia.basescan.org/tx/")


# ---------- the math, written out here so nothing is taken on trust ----------

def _canonical(d: dict) -> bytes:
    return json.dumps(d, sort_keys=True, separators=(",", ":")).encode()


def call_hash(agent_id: str, pair: str, horizon: str, direction: str, confidence: float,
              submitted_at_iso: str, price_at_submit: float, salt: str | None) -> str:
    """A call's hash. Calls from before 2026-10-28 have no salt and use the original format."""
    fields = {"agent": agent_id, "pair": pair, "horizon": horizon, "direction": direction,
              "confidence": round(confidence, 4), "submitted_at": submitted_at_iso,
              "price_at_submit": price_at_submit}
    if salt is not None:
        fields["salt"] = salt
    return hashlib.sha256(_canonical(fields)).hexdigest()


def merkle_root(hashes: list[str]) -> str:
    """RFC 6962 shape: leaves prefixed 0x00, nodes 0x01, odd node carried up, hashes sorted."""
    if not hashes:
        return hashlib.sha256(b"").hexdigest()
    layer = [hashlib.sha256(b"\x00" + bytes.fromhex(h)).digest() for h in sorted(hashes)]
    while len(layer) > 1:
        nxt = [hashlib.sha256(b"\x01" + layer[i] + layer[i + 1]).digest() for i in range(0, len(layer) - 1, 2)]
        if len(layer) % 2:
            nxt.append(layer[-1])
        layer = nxt
    return layer[0].hex()


def expected_payload(day: str, root: str) -> str:
    return f"signal-room:v1:{day}:{root}"


# ---------- the public chain ----------

def chain_lookup(tx_hash: str, rpc_url: str = RPC_URL) -> dict:
    """Read the transaction straight from a public node: what it carries, whether it succeeded, when."""
    def rpc(method, params):
        r = httpx.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
        r.raise_for_status()
        return r.json().get("result")
    tx = rpc("eth_getTransactionByHash", [tx_hash])
    if not tx:
        return {"found": False}
    receipt = rpc("eth_getTransactionReceipt", [tx_hash]) or {}
    block = rpc("eth_getBlockByNumber", [tx["blockNumber"], False]) or {}
    data = tx.get("input", "0x")
    return {
        "found": True,
        "payload": bytes.fromhex(data[2:]).decode("utf-8", "replace"),
        "success": receipt.get("status") == "0x1",
        "time": dt.datetime.fromtimestamp(int(block.get("timestamp", "0x0"), 16), dt.timezone.utc),
        "sender": tx.get("from"),
    }


# ---------- the check ----------

def verify_day(client, day: str, chain=chain_lookup, sample: int | None = None, pause: float = 1.1,
               progress=lambda done, total: None) -> dict:
    """Run all three checks for one closed day. `client` needs log(day) and read(signal_id)."""
    log = client.log(day)
    listed = log["signals"]
    out = {"day": day, "calls": len(listed), "anchored_tx": log.get("anchored_tx"),
           "checked": 0, "matched": 0, "mismatched": [], "still_open": 0}

    # Link 1: each call's hash from what it revealed.
    todo = listed if not sample or sample >= len(listed) else random.Random(day).sample(listed, sample)
    for i, item in enumerate(todo, 1):
        body = client.read(item["signal_id"])
        if body.get("result") is None:
            out["still_open"] += 1
        else:
            h = call_hash(body["agent_id"], body["pair"], body["horizon"], body["direction"], float(body["confidence"]),
                          body["submitted_at"], body["price_at_submit"], body.get("salt"))
            out["checked"] += 1
            if h == item["hash"] == body["hash"]:
                out["matched"] += 1
            else:
                out["mismatched"].append(item["signal_id"])
        progress(i, len(todo))
        if pause and i < len(todo):
            time.sleep(pause)

    # Link 2: the day's root from the published hashes.
    out["root"] = merkle_root([x["hash"] for x in listed])
    out["root_matches_anchor"] = out["root"] == log.get("anchored_root")

    # Link 3: the chain.
    out["chain"] = chain(log["anchored_tx"]) if log.get("anchored_tx") else {"found": False}
    out["chain_matches"] = bool(out["chain"].get("found") and out["chain"].get("success")
                                and out["chain"].get("payload") == expected_payload(day, out["root"]))
    out["ok"] = (not out["mismatched"]) and out["root_matches_anchor"] and out["chain_matches"]
    return out


def report(r: dict) -> str:
    lines = [f"Signal Room record for {r['day']}: {r['calls']} calls"]
    open_note = f" ({r['still_open']} still open, skipped)" if r["still_open"] else ""
    if r["mismatched"]:
        lines.append(f"  ✘ calls: {len(r['mismatched'])} of {r['checked']} do NOT match their published fingerprint: {', '.join(r['mismatched'][:5])}")
    else:
        lines.append(f"  ✔ calls: all {r['checked']} checked match their published fingerprint{open_note}")
    lines.append(f"  {'✔' if r['root_matches_anchor'] else '✘'} day: fingerprint rebuilt from every call "
                 f"{'matches' if r['root_matches_anchor'] else 'does NOT match'} the anchored one ({r['root'][:12]}…)")
    c = r["chain"]
    if not r["anchored_tx"]:
        lines.append("  – chain: this day has not been anchored yet")
    elif not c.get("found"):
        lines.append(f"  ✘ chain: transaction {r['anchored_tx'][:12]}… not found on the public chain")
    else:
        when = c["time"].astimezone().strftime("%b %d %I:%M %p").replace(" 0", " ")
        lines.append(f"  {'✔' if r['chain_matches'] else '✘'} chain: the public transaction "
                     f"{'carries exactly this fingerprint' if r['chain_matches'] else 'does NOT carry this fingerprint'}, written {when}")
        lines.append(f"    {EXPLORER}{r['anchored_tx']}")
    lines.append("VERIFIED: nothing in this day's record was changed after it was anchored." if r["ok"]
                 else "NOT VERIFIED: see the ✘ lines above.")
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    from .client import SignalRoomClient
    ap = argparse.ArgumentParser(prog="verify", description="Check one closed day of the room's record yourself.")
    ap.add_argument("day", help="YYYY-MM-DD (UTC), a day that has closed")
    ap.add_argument("--sample", type=int, default=None, help="check this many random calls instead of all")
    a = ap.parse_args(argv)
    base = os.getenv("SIGNAL_ROOM_URL", f"http://{os.getenv('SIGNAL_ROOM_HOST', '127.0.0.1')}:{os.getenv('SIGNAL_ROOM_PORT', '8000')}")
    state = os.path.join(os.path.dirname(__file__), "state", "verifier.json")
    client = SignalRoomClient.load_or_register(base, state, name="verifier")

    def progress(done, total):
        print(f"\r  checking calls: {done}/{total}", end="", flush=True)
    r = verify_day(client, a.day, sample=a.sample, progress=progress)
    print("\r" + " " * 40 + "\r" + report(r))
    sys.exit(0 if r["ok"] else 1)


if __name__ == "__main__":
    main(sys.argv[1:])

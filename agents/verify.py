"""Check the room's history yourself: `./start.sh verify 2026-11-03`.

For one closed day this checks three links, using its own math and only what any registered agent
can see, so it trusts nothing the room says:

  1. every closed call's hash, recomputed from what the call revealed when it closed (direction,
     confidence, time, salt; plus the start price for calls from before 2026-11-05), matches the
     hash the room published for it that day, and so does its leaf (hash + the start the room gave it);
  2. the day's Merkle root, rebuilt from those leaves, matches the root the room says it anchored;
  3. that root is exactly what the public blockchain transaction carries, the transaction was sent
     by the room's published anchor wallet, and when it was written is reported. The chain proves
     nothing changed SINCE that moment; an anchor written long after the day is said so.

If someone changed a call or its start price after the fact, link 1 breaks. If calls were added or
removed, link 2 breaks. If the published record was rewritten after anchoring, link 3 breaks.

A priced call (one side of a Kalshi market, judged against what it cost) gets a fourth check that
does not go through the room at all. First the room's own record of the call is held to the room's
published rules: the price was read after the call was stamped, the fill decision is what the rule
gives for the numbers recorded, the cost is the price plus Kalshi's fee. Then to Kalshi's own public
record: the market, its close, the result the call was scored by (or that there was none, for a void),
and that the price and bid the room says it saw sit inside what Kalshi's minute-by-minute history
shows for that side in that minute. Two things public data cannot settle and so rest on the room's
word: how many contracts were on offer, and where inside the minute's range the price stood at the
second the room looked. If Kalshi cannot be reached the day is NOT YET verified, never passed.

Calls still open are skipped (reading them would cost credits); a day is only fully checkable once
every call in it has closed. Reading a closed call is free. Requests are paced under the room's rate
limit, so a full day takes a few minutes; `--sample N` checks N random calls instead of all.
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
# The room's anchor wallet, published out of band (README). A transaction from any other sender
# proves nothing about this room. Override with SIGNAL_ROOM_ANCHOR_WALLET for another deployment.
ANCHOR_WALLET = os.getenv("SIGNAL_ROOM_ANCHOR_WALLET", "0xea11b5E115F0a6CE69FC3dA217e66d1199411A32")
LATE_ANCHOR_HOURS = 48   # an anchor written later than this after the day closed is flagged as late
HORIZON_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "24h": 86400}


# ---------- the math, written out here so nothing is taken on trust ----------

def _canonical(d: dict) -> bytes:
    return json.dumps(d, sort_keys=True, separators=(",", ":")).encode()


def call_hash(agent_id: str, pair: str, horizon: str, direction: str, confidence: float,
              submitted_at_iso: str, price_at_submit: float | None, salt: str | None,
              hash_version: int | None = None) -> str:
    """A call's hash. Calls from before 2026-10-28 have no salt (format 1); from 2026-11-05 (format 3)
    the start price is set after the post, so it is not in the hash."""
    fields = {"agent": agent_id, "pair": pair, "horizon": horizon, "direction": direction,
              "confidence": round(confidence, 4), "submitted_at": submitted_at_iso}
    if hash_version != 3:
        fields["price_at_submit"] = price_at_submit
    if salt is not None:
        fields["salt"] = salt
    return hashlib.sha256(_canonical(fields)).hexdigest()


def leaf_hash(call_hash_hex: str, start_at_iso: str, price_at_submit: float) -> str:
    """What the day's root commits to for a call with a start: receipt hash + start time + start price."""
    return hashlib.sha256(_canonical({"hash": call_hash_hex, "start_at": start_at_iso,
                                      "price_at_submit": price_at_submit})).hexdigest()


def priced_hash(agent_id: str, venue: str, market: str, side: str, limit: float, stated: float | None,
                submitted_at_iso: str, salt: str) -> str:
    """A priced call's hash (format 4): the market, the side, the most the agent would pay and its
    stated chance (None: it claimed nothing beyond the price), the server stamp and the salt."""
    return hashlib.sha256(_canonical({
        "agent": agent_id, "kind": "priced", "venue": venue, "market": market, "side": side,
        "limit": round(limit, 4), "confidence": None if stated is None else round(stated, 4),
        "submitted_at": submitted_at_iso, "salt": salt})).hexdigest()


def priced_leaf(call_hash_hex: str, salt: str, fill: dict) -> str:
    """What the day's root commits to for a priced call: receipt hash + salt + what the room saw at the venue."""
    return hashlib.sha256(_canonical({"hash": call_hash_hex, "salt": salt, "fill": fill})).hexdigest()


def leaf_of(item: dict) -> str:
    if item.get("kind") == "priced":
        # A priced call's leaf holds its salt, which stays sealed until the call closes, so the published
        # leaf is used here; link 1 recomputes it for every closed call from what the call revealed.
        return item.get("leaf") or item["hash"]
    return leaf_hash(item["hash"], item["start_at"], item["price_at_submit"]) if item.get("start_at") else item["hash"]


# ---------- the venue's own public record ----------
#
# Everything below is written out here, not imported from the room: its own reader, its own copy of
# Kalshi's fee and of the room's published fill rules. A mistake in the room's code is then not
# repeated by the check on it.

KALSHI_URL = os.getenv("SIGNAL_ROOM_KALSHI_URL", "https://api.elections.kalshi.com/trade-api/v2")
FILL_MIN_SIZE, FILL_MIN_PRICE, FILL_MAX_PRICE, FILL_MIN_SECONDS_LEFT = 10.0, 0.05, 0.95, 60
LIVE_READ_SECONDS = 20      # the longest a live look at Kalshi may trail the call's stamp
_kalshi_http: httpx.Client | None = None    # tests put a stand-in here


class VenueDown(Exception):
    """Kalshi could not be read just now. Nothing about the room follows from that."""


def kalshi_fee(price: float) -> float:
    """Kalshi's fee for one contract: 7% x price x (1 - price), rounded up to the cent."""
    from decimal import ROUND_CEILING, Decimal
    p = Decimal(str(round(price, 6)))
    return float((Decimal("7") * p * (1 - p)).to_integral_value(rounding=ROUND_CEILING)) / 100


def kalshi_cost(ask: float) -> float:
    return round(ask + kalshi_fee(ask), 6)


def kalshi_fade_bar(bid: float | None) -> float:
    if bid is None or bid <= 0:
        return 0.0
    other = 1.0 - bid
    return round(min(0.99, max(0.0, 1.0 - (other + kalshi_fee(other)))), 6)


def fill_rule(ask: float | None, size: float, limit: float, seconds_left: float) -> tuple[str, str]:
    """The room's published fill rule, restated: what a price, a size, a limit and the time left must give."""
    if ask is None:
        return "unfilled", "nothing on offer"
    if seconds_left < FILL_MIN_SECONDS_LEFT:
        return "unfilled", "under a minute left"
    if not (FILL_MIN_PRICE <= ask <= FILL_MAX_PRICE):
        return "unfilled", "price outside 5c-95c"
    if ask > limit + 1e-9:
        return "unfilled", "price above the limit"
    if size < FILL_MIN_SIZE:
        return "unfilled", "under 10 on offer"
    return "filled", ""


def _utc(text: str) -> dt.datetime:
    """A time from Kalshi (ending in Z) or from the room (already UTC, no zone) as plain UTC."""
    d = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return d.astimezone(dt.timezone.utc).replace(tzinfo=None) if d.tzinfo else d


class KalshiPublic:
    """Kalshi's public market data, read directly. Nothing here passes through the room's server."""

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        """The answer, or None when Kalshi says there is nothing at that address."""
        try:
            r = (_kalshi_http or httpx).get(KALSHI_URL + path, params=params, timeout=10)
        except httpx.HTTPError as e:
            raise VenueDown(type(e).__name__) from e
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise VenueDown(f"Kalshi answered {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise VenueDown("Kalshi's answer was not JSON") from e

    def market(self, ticker: str) -> dict | None:
        for path in (f"/markets/{ticker}", f"/historical/markets/{ticker}"):   # Kalshi moves old markets to its archive
            market = (self._get(path) or {}).get("market")
            if isinstance(market, dict):
                return market
        return None

    def minute(self, ticker: str, at: dt.datetime) -> dict | None:
        """Kalshi's one-minute record holding the moment `at` (naive UTC), or None if it has none."""
        end = (int(at.replace(tzinfo=dt.timezone.utc).timestamp()) // 60 + 1) * 60
        params = {"start_ts": end - 60, "end_ts": end, "period_interval": 1}
        for path in (f"/series/{ticker.split('-')[0]}/markets/{ticker}/candlesticks", f"/historical/markets/{ticker}/candlesticks"):
            for c in ((self._get(path, params) or {}).get("candlesticks") or []):
                if isinstance(c, dict) and c.get("end_period_ts") == end:
                    return c
        return None


def side_ranges(candle: dict, side: str) -> tuple[float, float, float, float]:
    """(lowest ask, highest ask, lowest bid, highest bid) for one side over one minute. NO mirrors YES:
    buying NO costs 1 minus the YES bid, and the best bid for NO is 1 minus the YES ask."""
    yb, ya = candle["yes_bid"], candle["yes_ask"]
    yb_lo, yb_hi, ya_lo, ya_hi = (float(yb["low_dollars"]), float(yb["high_dollars"]),
                                  float(ya["low_dollars"]), float(ya["high_dollars"]))
    if side == "yes":
        return ya_lo, ya_hi, yb_lo, yb_hi
    return round(1 - yb_hi, 6), round(1 - yb_lo, 6), round(1 - ya_hi, 6), round(1 - ya_lo, 6)


def _same(a, b) -> bool:
    return (a is None and b is None) or (a is not None and b is not None and abs(float(a) - float(b)) < 1e-6)


def check_priced(body: dict, venue) -> tuple[str | None, bool]:
    """(what is wrong, whether Kalshi's record was consulted) for one closed priced call. First the room's
    own record of the call is held to the room's own published rules; then to Kalshi's public record.
    Raises VenueDown if Kalshi cannot be read. Public data cannot settle everything, and what it cannot
    stays on the room's word: how many contracts were on offer at the price on a live look, and where
    inside its minute's range the price stood at the second the room looked."""
    fill, result, side = body.get("fill"), body.get("result"), body.get("side")
    posted = dt.datetime.fromisoformat(body["submitted_at"])
    if not fill:
        # The room says it never established a price. Only a void can follow from that.
        return (None if result == "void" else f"result {result!r} with no record of a price"), False
    status, source = fill.get("fill"), fill.get("source")
    ask, bid, size = fill.get("ask"), fill.get("bid"), float(fill.get("size") or 0.0)
    if ask is not None and not 0.0 < float(ask) < 1.0:
        return f"a price of {ask!r} is not a price", False
    at = dt.datetime.fromisoformat(fill["at"])
    closes = dt.datetime.fromisoformat(body["resolve_at"])

    # The look must follow the call, and promptly; a fill from history is stamped at the call itself.
    if source == "live" and not (posted <= at <= posted + dt.timedelta(seconds=LIVE_READ_SECONDS)):
        return f"the price was read at {fill['at']}, the call was stamped {body['submitted_at']}", False
    if source == "history" and at != posted:
        return "a fill from history must be stamped at the call's own time", False
    if source not in ("live", "history"):
        return f"unknown fill source {source!r}", False

    # The fill decision must be what the published rule gives for the numbers the room recorded.
    reason = fill.get("reason") or ""
    venue_reasons = ("no such market", "market was not open")      # decided from Kalshi's record, checked below
    if not (status == "unfilled" and reason in venue_reasons):
        expected = fill_rule(None if ask is None else float(ask), size, float(body["limit"]), (closes - at).total_seconds())
        if (status, reason) != expected:
            return f"recorded as {status} ({reason or 'no reason'}), the fill rule gives {expected[0]} ({expected[1] or 'no reason'})", False
    if status == "filled":
        if not _same(fill.get("bar"), kalshi_cost(float(ask))):
            return f"cost recorded as {fill.get('bar')}, price {ask} plus Kalshi's fee is {kalshi_cost(float(ask))}", False
        if not _same(fill.get("fade_bar"), kalshi_fade_bar(None if bid is None else float(bid))):
            return f"fade bar recorded as {fill.get('fade_bar')}, the bid {bid} gives {kalshi_fade_bar(bid)}", False
        if result not in ("right", "wrong", "void"):
            return f"a filled call closed as {result!r}", False
    elif status == "unfilled":
        if result != "unfilled":
            return f"an unfilled call closed as {result!r}", False
        if fill.get("bar") is not None:
            return "an unfilled call has a cost", False
    else:
        return f"unknown fill status {status!r}", False

    # The score must follow from the stated chance, the cost and the result (none of them is in the leaf).
    if status == "filled" and result in ("right", "wrong"):
        y = 1.0 if result == "right" else 0.0
        bar, stated = float(fill["bar"]), body.get("stated")
        scored = bar if stated is None else max(float(stated), bar)     # a chance below the cost claims nothing
        if not _same(body.get("brier"), round((scored - y) ** 2, 6)):
            return f"Brier recorded as {body.get('brier')}, the stated chance and the result give {round((scored - y) ** 2, 6)}", False
        if not _same(body.get("net_return"), round(y - bar, 6)):
            return f"profit recorded as {body.get('net_return')}, the result less the cost is {round(y - bar, 6)}", False

    # Kalshi's own record. Anything odd in Kalshi's answers is Kalshi's, not the room's: the call is then
    # left unchecked (NOT YET), never failed and never passed.
    try:
        return _against_kalshi(body, venue, fill, posted, at, closes), True
    except VenueDown:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as e:
        raise VenueDown(f"Kalshi's record of this market could not be read ({type(e).__name__})") from e


def _against_kalshi(body: dict, venue, fill: dict, posted: dt.datetime, at: dt.datetime, closes: dt.datetime) -> str | None:
    status, source, reason = fill.get("fill"), fill.get("source"), fill.get("reason") or ""
    ask, bid, size = fill.get("ask"), fill.get("bid"), float(fill.get("size") or 0.0)
    result, side = body.get("result"), body.get("side")
    market = venue.market(body["market"])
    if market is None:
        if reason == "no such market":
            return None
        raise VenueDown("Kalshi no longer serves this market")       # archived, or never there: cannot be told apart
    if reason == "no such market":
        return "recorded as 'no such market', and Kalshi has it"
    opens, real_close = _utc(market["open_time"]), _utc(market["close_time"])
    if reason == "market was not open":
        return None if not (opens <= posted < real_close) else "recorded as 'market was not open', and it was"
    if not (opens <= posted < real_close):
        return "the call was stamped outside the market's trading window"
    if abs((closes - real_close).total_seconds()) > 2:
        # Kalshi can move a close. That matters only if the fill decision would have been another one.
        again = fill_rule(None if ask is None else float(ask), size, float(body["limit"]), (real_close - at).total_seconds())
        if again != (status, reason):
            return f"the room closed the call at {body['resolve_at']}, Kalshi's market closed at {market['close_time']}"
    final = market.get("result") if market.get("status") == "finalized" else None
    if status == "filled":
        if result == "void":
            if final in ("yes", "no"):
                return f"voided, though Kalshi's result is {final}"
        else:
            if final != body.get("venue_result"):
                return f"Kalshi's result is {final!r}, the room scored it as {body.get('venue_result')!r}"
            if (result == "right") != (final == side):
                return f"scored {result} for {side} though Kalshi's result is {final}"

    # Every minute the look could have fallen in: from the call's stamp to the moment the room says it read.
    moments = {posted} if source == "history" else {posted, at, at - dt.timedelta(seconds=5), at + dt.timedelta(seconds=5)}
    candles = {}
    for moment in moments:
        candle = venue.minute(body["market"], moment)
        if candle is not None:
            candles[candle["end_period_ts"]] = candle
    if not candles:
        raise VenueDown("Kalshi has no record of that minute")
    ranges = [side_ranges(c, side) for c in candles.values()]
    always_offered = all(0.0 < lo and hi < 1.0 for lo, hi, _, _ in ranges)
    always_bid = all(0.0 < lo and hi < 1.0 for _, _, lo, hi in ranges)
    shown = f"{min(r[0] for r in ranges)} to {max(r[1] for r in ranges)}"
    if ask is None:
        return f"recorded as nothing on offer; Kalshi shows offers of {shown} throughout" if always_offered else None
    if bid is None and always_bid:
        return f"recorded with no bid; Kalshi shows bids of {min(r[2] for r in ranges)} to {max(r[3] for r in ranges)} throughout"
    for candle, (ask_lo, ask_hi, bid_lo, bid_hi) in zip(candles.values(), ranges):
        if source == "history":
            worst_bid = bid_lo if 0.0 < bid_lo < 1.0 else None
            if abs(float(ask) - ask_hi) < 1e-6 and _same(bid, worst_bid):
                if not _same(size, float(candle["volume_fp"])):
                    return f"recorded {size} contracts traded that minute, Kalshi's record says {candle['volume_fp']}"
                return None                           # the worst price of the minute, as the rule says
        elif ask_lo - 1e-6 <= float(ask) <= ask_hi + 1e-6 and (bid is None or bid_lo - 1e-6 <= float(bid) <= bid_hi + 1e-6):
            return None
    return f"the room says the price was {ask} (bid {bid}), Kalshi's record of that minute shows {shown}"


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

def is_open(item: dict, now: dt.datetime) -> bool:
    """From the listing alone. The listing says whether a call has closed; if the room lied, the read
    would show no result and the call counts as open anyway (the verdict is then NOT YET, never VERIFIED)."""
    if "resolved" in item:
        return not item["resolved"]
    return dt.datetime.fromisoformat(item["resolve_at"]) + dt.timedelta(seconds=120) > now


def verify_day(client, day: str, chain=chain_lookup, sample: int | None = None, pause: float = 1.1,
               progress=lambda done, total: None, now: dt.datetime | None = None,
               anchor_wallet: str | None = ANCHOR_WALLET, venue=None) -> dict:
    """Run every check for one closed day. `client` needs log(day) and read(signal_id); `venue` needs
    market(ticker) and minute(ticker, at) and defaults to Kalshi's public data."""
    venue = venue or KalshiPublic()
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    log = client.log(day)
    listed = log["signals"]
    out = {"day": day, "calls": len(listed), "anchored_tx": log.get("anchored_tx"),
           "checked": 0, "matched": 0, "mismatched": [], "still_open": 0,
           "venue_checked": 0, "venue_mismatched": [], "venue_unreachable": 0, "venue_unseen": 0}

    # Link 1: each call's hash and leaf from what it revealed. Open calls are skipped, never read:
    # reading an open call costs credits and the verifier is not here to buy anything.
    closed = [x for x in listed if not is_open(x, now)]
    out["still_open"] = len(listed) - len(closed)
    todo = closed if not sample or sample >= len(closed) else random.Random(day).sample(closed, sample)
    live_looks, unread = [], []      # when the room read Kalshi live; calls it says it could not get a price for
    for i, item in enumerate(todo, 1):
        body = client.read(item["signal_id"])
        if body.get("result") is None:
            out["still_open"] += 1
        else:
            if body.get("kind") == "priced":
                h = priced_hash(body["agent_id"], body["venue"], body["market"], body["side"], float(body["limit"]),
                                body.get("stated"), body["submitted_at"], body["salt"])
                lf = priced_leaf(h, body["salt"], body["fill"]) if body.get("fill") else h
                try:
                    wrong, consulted = check_priced(body, venue)
                    out["venue_checked"] += consulted
                    out["venue_unseen"] += (not consulted) and not wrong
                    fill = body.get("fill") or {}
                    if fill.get("source") == "live":
                        live_looks.append(dt.datetime.fromisoformat(fill["at"]))
                    elif not fill and not wrong:
                        unread.append((item["signal_id"], dt.datetime.fromisoformat(body["submitted_at"])))
                    if wrong:
                        out["venue_mismatched"].append(f"{item['signal_id']}: {wrong}")
                except VenueDown:
                    out["venue_unreachable"] += 1
                except Exception as e:      # a record that cannot even be read is a failed check, never a skipped one
                    out["venue_mismatched"].append(f"{item['signal_id']}: the room's record of this call cannot be checked ({type(e).__name__})")
            else:
                h = call_hash(body["agent_id"], body["pair"], body["horizon"], body["direction"], float(body["confidence"]),
                              body["submitted_at"], body["price_at_submit"], body.get("salt"), body.get("hash_version"))
                lf = leaf_hash(h, body["start_at"], body["price_at_submit"]) if body.get("start_at") else h
            out["checked"] += 1
            if h == item["hash"] == body["hash"] and lf == item.get("leaf", item["hash"]):
                out["matched"] += 1
            else:
                out["mismatched"].append(item["signal_id"])
        progress(i, len(todo))
        if pause and i < len(todo):
            time.sleep(pause)

    # A call voided for want of a price is the one record Kalshi cannot contradict, so it is held to the
    # room's own day: "could not get a price for half an hour" does not sit with live looks in that half hour.
    for sid, posted in unread:
        near = [t for t in live_looks if posted <= t <= posted + dt.timedelta(seconds=1800)]
        if near:
            out["venue_unseen"] -= 1
            out["venue_mismatched"].append(f"{sid}: voided for want of a price, though the room read Kalshi live {len(near)} "
                                           f"times in the half hour that followed")

    # Link 2: the day's root from the published leaves.
    out["root"] = merkle_root([leaf_of(x) for x in listed])
    out["root_matches_anchor"] = out["root"] == log.get("anchored_root")

    # Link 3: the chain.
    out["chain"] = chain(log["anchored_tx"]) if log.get("anchored_tx") else {"found": False}
    c = out["chain"]
    out["chain_matches"] = bool(c.get("found") and c.get("success") and c.get("payload") == expected_payload(day, out["root"]))
    sender = (c.get("sender") or "").lower()
    out["sender_matches"] = bool(c.get("found")) and (anchor_wallet is None or sender == anchor_wallet.lower())
    out["anchored_at"] = c.get("time")
    day_end = dt.datetime.fromisoformat(day) + dt.timedelta(days=1)
    out["anchor_late_hours"] = None
    if out["anchored_at"] is not None:
        at = out["anchored_at"].replace(tzinfo=None) if out["anchored_at"].tzinfo else out["anchored_at"]
        late = (at - day_end).total_seconds() / 3600
        out["anchor_late_hours"] = round(late, 1) if late > LATE_ANCHOR_HOURS else 0
    # The record is intact when every link holds. It is VERIFIED only when, on top of that, every priced
    # call could be held against Kalshi: an unreachable Kalshi leaves the day unfinished, not passed.
    out["intact"] = ((not out["mismatched"]) and out["root_matches_anchor"] and out["chain_matches"]
                     and out["sender_matches"] and not out["venue_mismatched"])
    out["ok"] = out["intact"] and out["still_open"] == 0 and out["venue_unreachable"] == 0
    return out


def report(r: dict) -> str:
    lines = [f"Signal Room record for {r['day']}: {r['calls']} calls"]
    open_note = f" ({r['still_open']} still open, skipped)" if r["still_open"] else ""
    if r["mismatched"]:
        lines.append(f"  ✘ calls: {len(r['mismatched'])} of {r['checked']} do NOT match their published fingerprint: {', '.join(r['mismatched'][:5])}")
    else:
        lines.append(f"  ✔ calls: all {r['checked']} checked match their published fingerprint{open_note}")
    if r.get("venue_mismatched"):
        lines.append(f"  ✘ Kalshi: {len(r['venue_mismatched'])} priced calls do NOT match Kalshi's own public record: "
                     + "; ".join(r["venue_mismatched"][:3]))
    elif r.get("venue_checked"):
        lines.append(f"  ✔ Kalshi: all {r['venue_checked']} priced calls checked match Kalshi's own record (result, close, "
                     "and the price seen that minute); the size on offer rests on the room's word")
    if r.get("venue_unseen"):
        lines.append(f"  ⚠ Kalshi: {r['venue_unseen']} priced calls were voided for want of a price; Kalshi's record cannot confirm or deny that")
    if r.get("venue_unreachable"):
        lines.append(f"  – Kalshi: could not be reached for {r['venue_unreachable']} priced calls; their fills and results were not re-checked")
    if r["anchored_tx"]:
        lines.append(f"  {'✔' if r['root_matches_anchor'] else '✘'} day: fingerprint rebuilt from every call "
                     f"{'matches' if r['root_matches_anchor'] else 'does NOT match'} the anchored one ({r['root'][:12]}…)")
    else:
        lines.append(f"  – day: fingerprint rebuilt from every call is {r['root'][:12]}…; nothing anchored to compare it with yet")
    c = r["chain"]
    if not r["anchored_tx"]:
        lines.append("  – chain: this day has not been anchored yet")
    elif not c.get("found"):
        lines.append(f"  ✘ chain: transaction {r['anchored_tx'][:12]}… not found on the public chain")
    else:
        when = c["time"].astimezone().strftime("%b %d %I:%M %p").replace(" 0", " ")
        lines.append(f"  {'✔' if r['chain_matches'] else '✘'} chain: the public transaction "
                     f"{'carries exactly this fingerprint' if r['chain_matches'] else 'does NOT carry this fingerprint'}, written {when}")
        if not r["sender_matches"]:
            lines.append(f"  ✘ chain: it was sent by {c.get('sender')}, not the room's published anchor wallet")
        if r["anchor_late_hours"]:
            lines.append(f"  ⚠ chain: anchored {r['anchor_late_hours'] / 24:.1f} days after the day closed; the record could have been edited before then")
        lines.append(f"    {EXPLORER}{r['anchored_tx']}")
    if not r["anchored_tx"] and not r["mismatched"] and not r.get("venue_mismatched"):
        lines.append("NOT YET: this day has not been anchored; until it is, nothing outside the room holds it to this record.")
    elif r["ok"]:
        since = c["time"].astimezone().strftime("%b %d %I:%M %p").replace(" 0", " ")
        lines.append(f"VERIFIED: nothing in this day's record has changed since it was anchored ({since}).")
    elif r.get("intact", True) and r["still_open"] and not r["mismatched"] and r["root_matches_anchor"] and r["chain_matches"] and r["sender_matches"]:
        lines.append(f"NOT YET: {r['still_open']} calls are still open; check again once the day has fully closed.")
    elif r.get("intact") and r.get("venue_unreachable"):
        lines.append(f"NOT YET: the record is intact, but {r['venue_unreachable']} priced calls could not be held against Kalshi; check again when Kalshi can be reached.")
    else:
        lines.append("NOT VERIFIED: see the ✘ lines above.")
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

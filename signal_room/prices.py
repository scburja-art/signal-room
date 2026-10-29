"""Reference prices: median of Coinbase, Kraken, and Bitstamp spot, sampled once a minute.

Every raw feed value is stored with the feed's own timestamp so any score can be replayed.
A feed that is down is skipped; the median is taken over whatever answered. If fewer than
two feeds answer, no reference price is written for that minute.
"""
from __future__ import annotations

import datetime as dt
import statistics

import httpx
from sqlalchemy import desc
from sqlalchemy.orm import Session

from . import config
from .db import AuditEvent, PriceSnapshot, ReferencePrice, utcnow

TIMEOUT = 8.0

KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD"}


def fetch_coinbase(client: httpx.Client, pair: str) -> tuple[float, dt.datetime | None]:
    r = client.get(f"https://api.exchange.coinbase.com/products/{pair}-USD/ticker", timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    t = dt.datetime.fromisoformat(j["time"].replace("Z", "+00:00")).replace(tzinfo=None) if "time" in j else None
    return float(j["price"]), t


def fetch_kraken(client: httpx.Client, pair: str) -> tuple[float, dt.datetime | None]:
    r = client.get("https://api.kraken.com/0/public/Ticker", params={"pair": KRAKEN_PAIRS[pair]}, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(str(j["error"]))
    result = next(iter(j["result"].values()))
    return float(result["c"][0]), None


def fetch_bitstamp(client: httpx.Client, pair: str) -> tuple[float, dt.datetime | None]:
    r = client.get(f"https://www.bitstamp.net/api/v2/ticker/{pair.lower()}usd/", timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    t = dt.datetime.fromtimestamp(int(j["timestamp"]), dt.timezone.utc).replace(tzinfo=None) if "timestamp" in j else None
    return float(j["last"]), t


FEEDS = {"coinbase": fetch_coinbase, "kraken": fetch_kraken, "bitstamp": fetch_bitstamp}


def sample_all(db: Session, client: httpx.Client | None = None, now: dt.datetime | None = None) -> dict[str, float]:
    """Fetch every feed for every pair, store raw snapshots, write the medians. Returns {pair: median}."""
    now = now or utcnow().replace(second=0, microsecond=0)
    own_client = client is None
    client = client or httpx.Client()
    medians: dict[str, float] = {}
    try:
        for pair in config.PAIRS:
            if db.query(ReferencePrice.id).filter_by(pair=pair, sampled_at=now).first():
                continue  # already sampled this minute
            prices = []
            for feed, fn in FEEDS.items():
                try:
                    price, feed_time = fn(client, pair)
                except Exception as e:  # feed down: skip it, log it
                    db.add(AuditEvent(event="feed_error", detail=f"{feed} {pair}: {type(e).__name__}"))
                    continue
                db.add(PriceSnapshot(pair=pair, feed=feed, price=price, feed_time=feed_time, sampled_at=now))
                prices.append(price)
            if len(prices) >= 2:
                med = statistics.median(prices)
                db.add(ReferencePrice(pair=pair, price=med, feeds_used=len(prices), sampled_at=now))
                medians[pair] = med
            else:
                db.add(AuditEvent(event="price_gap", detail=f"{pair} {now.isoformat()} feeds={len(prices)}"))
        db.commit()
    finally:
        if own_client:
            client.close()
    return medians


# ---------- backfill from minute candles ----------
#
# When the machine sleeps, the sampler stops and signals that come due in the gap have no
# reference price. Rather than void them, reconstruct the missing minutes from the exchanges'
# own 1-minute candles. A candle opening at T closes at T+60s, and the live sampler records the
# ticker at the top of each minute, so candle(T).close is stored as the price at T+60s.

def _iso(t: dt.datetime) -> str:
    return t.replace(microsecond=0).isoformat() + "Z"


def candles_coinbase(client: httpx.Client, pair: str, start: dt.datetime, end: dt.datetime) -> dict[dt.datetime, float]:
    r = client.get(f"https://api.exchange.coinbase.com/products/{pair}-USD/candles",
                   params={"granularity": 60, "start": _iso(start), "end": _iso(end)}, timeout=TIMEOUT)
    r.raise_for_status()
    out = {}
    for t, lo, hi, op, cl, vol in r.json():
        out[dt.datetime.fromtimestamp(int(t), dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(seconds=60)] = float(cl)
    return out


def candles_kraken(client: httpx.Client, pair: str, start: dt.datetime, end: dt.datetime) -> dict[dt.datetime, float]:
    since = int(start.replace(tzinfo=dt.timezone.utc).timestamp()) - 60
    r = client.get("https://api.kraken.com/0/public/OHLC", params={"pair": KRAKEN_PAIRS[pair], "interval": 1, "since": since}, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(str(j["error"]))
    rows = next(v for k, v in j["result"].items() if k != "last")
    out = {}
    for row in rows:
        t = dt.datetime.fromtimestamp(int(row[0]), dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(seconds=60)
        if start <= t <= end + dt.timedelta(seconds=60):
            out[t] = float(row[4])
    return out


def candles_bitstamp(client: httpx.Client, pair: str, start: dt.datetime, end: dt.datetime) -> dict[dt.datetime, float]:
    since = int(start.replace(tzinfo=dt.timezone.utc).timestamp()) - 60
    r = client.get(f"https://www.bitstamp.net/api/v2/ohlc/{pair.lower()}usd/",
                   params={"step": 60, "limit": 1000, "start": since}, timeout=TIMEOUT)
    r.raise_for_status()
    out = {}
    for c in r.json().get("data", {}).get("ohlc", []):
        t = dt.datetime.fromtimestamp(int(c["timestamp"]), dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(seconds=60)
        if start <= t <= end + dt.timedelta(seconds=60):
            out[t] = float(c["close"])
    return out


CANDLE_FEEDS = {"coinbase": candles_coinbase, "kraken": candles_kraken, "bitstamp": candles_bitstamp}


def backfill(db: Session, pair: str, start: dt.datetime, end: dt.datetime, client: httpx.Client | None = None) -> int:
    """Fill missing minutes for one pair between start and end from exchange candles. Returns rows written."""
    start = start.replace(second=0, microsecond=0)
    end = end.replace(second=0, microsecond=0)
    if end < start:
        return 0
    existing = {r.sampled_at for r in db.query(ReferencePrice.sampled_at).filter(
        ReferencePrice.pair == pair, ReferencePrice.sampled_at >= start, ReferencePrice.sampled_at <= end)}
    wanted = []
    t = start
    while t <= end:
        if t not in existing:
            wanted.append(t)
        t += dt.timedelta(minutes=1)
    if not wanted:
        return 0
    own_client = client is None
    client = client or httpx.Client()
    per_feed: dict[str, dict[dt.datetime, float]] = {}
    try:
        for feed, fn in CANDLE_FEEDS.items():
            try:
                per_feed[feed] = fn(client, pair, start, end)
            except Exception as e:
                db.add(AuditEvent(event="backfill_feed_error", detail=f"{feed} {pair}: {type(e).__name__}"))
    finally:
        if own_client:
            client.close()
    written = 0
    for t in wanted:
        vals = [feed[t] for feed in per_feed.values() if t in feed]
        if len(vals) >= 2:
            db.add(ReferencePrice(pair=pair, price=statistics.median(vals), feeds_used=len(vals), sampled_at=t, source="backfill"))
            written += 1
    if written:
        db.add(AuditEvent(event="gap_backfilled", detail=f"{pair} {start.isoformat()}..{end.isoformat()} rows={written}"))
    db.commit()
    return written


def latest(db: Session, pair: str, max_age_seconds: int | None = None) -> ReferencePrice | None:
    row = db.query(ReferencePrice).filter_by(pair=pair).order_by(desc(ReferencePrice.sampled_at)).first()
    if row is None:
        return None
    if max_age_seconds is not None and (utcnow() - row.sampled_at).total_seconds() > max_age_seconds:
        return None
    return row


def nearest(db: Session, pair: str, when: dt.datetime, tolerance_seconds: int) -> ReferencePrice | None:
    lo = when - dt.timedelta(seconds=tolerance_seconds)
    hi = when + dt.timedelta(seconds=tolerance_seconds)
    rows = db.query(ReferencePrice).filter(
        ReferencePrice.pair == pair, ReferencePrice.sampled_at >= lo, ReferencePrice.sampled_at <= hi
    ).all()
    if not rows:
        return None
    return min(rows, key=lambda r: abs((r.sampled_at - when).total_seconds()))

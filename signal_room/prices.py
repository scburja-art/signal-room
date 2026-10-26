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

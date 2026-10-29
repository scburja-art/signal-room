"""Background jobs: sample prices every minute, resolve due signals, build the daily log, purge nonces."""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time

from . import anchor, auth, config, prices, service
from .db import AuditEvent, SessionLocal, utcnow

log = logging.getLogger("signal_room.scheduler")


_last_tick: list[float] = []


def tick() -> dict:
    """One pass of every job. Safe to call directly (tests, manual runs)."""
    db = SessionLocal()
    out = {}
    try:
        now_m = time.monotonic()
        if _last_tick and now_m - _last_tick[0] > 3 * config.PRICE_SAMPLE_SECONDS:
            gap = int(now_m - _last_tick[0])
            log.warning("gap of %ss since last tick (sleep?); due signals will backfill prices from exchange candles", gap)
            db.add(AuditEvent(event="tick_gap", detail=f"{gap}s"))
            db.commit()
        _last_tick[:] = [now_m]
        try:
            out["prices"] = len(prices.sample_all(db))
        except Exception as e:
            log.warning("price sample failed: %s", e)
            out["prices"] = 0
        out["resolved"] = service.resolve_due(db)
        yesterday = (utcnow() - dt.timedelta(days=1)).date()
        ylog = service.build_daily_log(db, yesterday)
        service.build_daily_log(db, utcnow().date())
        # Yesterday is closed, so its root is final: anchor it once. Today's root keeps changing.
        if anchor.enabled() and not ylog.anchored_tx:
            try:
                out["anchored"] = anchor.anchor_day(db, ylog)
            except Exception as e:
                log.warning("anchor failed for %s: %s", yesterday, e)
        auth.purge_old(db)
    finally:
        db.close()
    return out


def _loop(stop: threading.Event) -> None:
    while not stop.is_set():
        started = time.monotonic()
        try:
            result = tick()
            log.info("tick %s", result)
        except Exception as e:  # never let the loop die
            log.exception("tick failed: %s", e)
        elapsed = time.monotonic() - started
        stop.wait(max(1.0, config.PRICE_SAMPLE_SECONDS - elapsed))


def start() -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=_loop, args=(stop,), name="signal-room-scheduler", daemon=True).start()
    return stop

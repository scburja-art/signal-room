"""Background jobs: sample prices every minute, resolve due signals, build the daily log, purge nonces."""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time

from . import anchor, auth, config, prices, service
from sqlalchemy import func

from .db import AuditEvent, DailyLog, SessionLocal, Signal, utcnow

log = logging.getLogger("signal_room.scheduler")


_last_tick: list[float] = []


def oldest_unanchored(db, today: dt.date) -> dt.date | None:
    """The earliest closed day, from the first signal ever posted to yesterday, not yet anchored.
    Days with no calls count too: an anchored empty root proves nothing was slipped in later."""
    first = db.query(func.min(Signal.submitted_at)).scalar()
    if first is None:
        return None
    done = {d for (d,) in db.query(DailyLog.day).filter(DailyLog.anchored_tx.isnot(None))}
    day = first.date()
    while day < today:
        if day.isoformat() not in done:
            return day
        day += dt.timedelta(days=1)
    return None


def tick(anchor_inline: bool = True) -> dict:
    """One pass of every job. Safe to call directly (tests, manual runs). The running loop anchors in a
    background thread instead (anchor_inline=False) so a slow chain never stalls price sampling."""
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
        out["started"] = service.fill_start_prices(db)
        out["resolved"] = service.resolve_due(db)
        today = utcnow().date()
        service.build_daily_log(db, today - dt.timedelta(days=1))
        service.build_daily_log(db, today)
        if anchor_inline:
            out["anchored"] = anchor_step(db)
        auth.purge_old(db)
    finally:
        db.close()
    return out


def anchor_step(db) -> str | None:
    """A closed day's root is final, so anchor it once. Today's root keeps changing. One day per call,
    oldest first, so a laptop that was off for days (or a wallet added late) catches up on its own."""
    if not anchor.enabled():
        return None
    today = utcnow().date()
    day = oldest_unanchored(db, today)
    if day is None:
        return None
    if not service.day_ready_to_anchor(db, day):
        log.info("not anchoring %s yet: a call posted that day has no start price", day)
        return None
    try:
        return anchor.anchor_day(db, service.build_daily_log(db, day))
    except Exception as e:
        log.warning("anchor failed for %s: %s", day, e)
        return None


_anchor_lock = threading.Lock()


def _anchor_in_background() -> None:
    """Chain calls can take minutes; they must never hold up price sampling, so they run beside the tick."""
    if not _anchor_lock.acquire(blocking=False):
        return   # the previous anchor call is still talking to the chain
    def run():
        db = SessionLocal()
        try:
            anchor_step(db)
        finally:
            db.close()
            _anchor_lock.release()
    threading.Thread(target=run, name="signal-room-anchor", daemon=True).start()


def _loop(stop: threading.Event) -> None:
    while not stop.is_set():
        started = time.monotonic()
        try:
            result = tick(anchor_inline=False)
            if anchor.enabled():
                _anchor_in_background()
            log.info("tick %s", result)
        except Exception as e:  # never let the loop die
            log.exception("tick failed: %s", e)
        elapsed = time.monotonic() - started
        stop.wait(max(1.0, config.PRICE_SAMPLE_SECONDS - elapsed))


def start() -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=_loop, args=(stop,), name="signal-room-scheduler", daemon=True).start()
    return stop

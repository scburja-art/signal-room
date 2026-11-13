"""A read-only window onto Kalshi's public market data. No keys, no orders, nothing written to Kalshi.

The room uses it for three things about a priced call: the price on offer the moment the call lands,
the worst price of that minute when it could not look in time, and Kalshi's own result when the
market closes. Everything here fails safe: an answer that is missing, late or not understood raises
VenueError, and the caller then leaves the call unfilled or waiting. It is never guessed.

Only Kalshi's 15-minute up-or-down markets on the room's four coins are read (rule set 4, first
version). Prices are dollars per contract, between 0 and 1, exactly as Kalshi sends them.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

import httpx

VENUE = "kalshi"
BASE_URL = os.getenv("SIGNAL_ROOM_KALSHI_URL", "https://api.elections.kalshi.com/trade-api/v2")
READ_TIMEOUT = 5.0          # seconds the room waits for a live price before falling back to history
TAKER_RATE = Decimal("0.07")   # Kalshi fee schedule (effective 2026-07-07): round up(0.07 x C x P x (1 - P))
SERIES_CHECK_SECONDS = 3600  # how long one look at a series' fee settings is trusted

_TICKER = re.compile(r"^(KX(BTC|ETH|SOL|XRP)15M)-[0-9A-Z]{6,16}-[0-9A-Z]{1,6}$")


class VenueError(Exception):
    """Kalshi answered, and the answer cannot be used: a field is missing, a value makes no sense, the
    fee rule is not the one the room knows. Trying again will not help until someone looks."""


class VenueUnreachable(VenueError):
    """Kalshi could not be read just now: a busy or failing server, a refusal. The same read may well
    work in a minute."""


class VenueOffline(VenueUnreachable):
    """No answer at all: no connection, or a timeout. Asking for the next market would only wait again."""


class NoSuchMarket(VenueError):
    """Kalshi says there is no market with that ticker."""


class NoResult(VenueError):
    """Kalshi finalised the market with a result that is neither yes nor no. It can never be scored."""


def parse_ticker(ticker: str) -> tuple[str, str, str] | None:
    """(series, pair, horizon) for a market the room reads, else None. KXBTC15M-26NOV111345-45 →
    ('KXBTC15M', 'BTC', '15m')."""
    m = _TICKER.match(ticker) if isinstance(ticker, str) else None
    return (m.group(1), m.group(2), "15m") if m else None


def fee(price: float) -> float:
    """Kalshi's fee for buying one contract at this price, in dollars: 7% x price x (1 - price), rounded
    up to the cent (how it lands on an ordinary account buying a single contract)."""
    p = Decimal(str(round(price, 6)))
    cents = (TAKER_RATE * 100 * p * (1 - p)).to_integral_value(rounding=ROUND_CEILING)
    return float(cents) / 100


def cost(ask: float) -> float:
    """What one contract really costs: the price on offer plus the fee. This is the call's bar."""
    return round(ask + fee(ask), 6)


def fade_bar(bid: float | None) -> float:
    """The bar for betting against a call: 1 minus what the other side would have cost (its price is
    1 - bid, plus the fee on that). 0 when nobody was bidding, so fading was not possible."""
    if bid is None or bid <= 0:
        return 0.0
    other = 1.0 - bid
    return round(min(0.99, max(0.0, 1.0 - (other + fee(other)))), 6)


# ---------- reading ----------

_http: httpx.Client | None = None
_http_lock = threading.Lock()


def _client() -> httpx.Client:
    global _http
    with _http_lock:
        if _http is None:
            _http = httpx.Client(timeout=httpx.Timeout(READ_TIMEOUT, connect=3.0, pool=3.0),
                                 headers={"User-Agent": "signal-room (read-only market data)"})
        return _http


def _get(path: str, params: dict | None = None, http: httpx.Client | None = None, timeout: float | None = None) -> dict:
    try:
        r = (http or _client()).get(BASE_URL + path, params=params, **({"timeout": timeout} if timeout else {}))
    except httpx.HTTPError as e:
        raise VenueOffline(f"could not reach Kalshi: {type(e).__name__}") from e
    if r.status_code == 404:
        raise NoSuchMarket(path)
    if r.status_code != 200:
        raise VenueUnreachable(f"Kalshi answered {r.status_code} for {path}")
    try:
        body = r.json()
    except ValueError as e:
        raise VenueError("Kalshi's answer was not JSON") from e
    if not isinstance(body, dict):
        raise VenueError("Kalshi's answer was not an object")
    return body


def _dollars(value) -> float:
    if isinstance(value, bool):
        raise VenueError(f"not a price: {value!r}")
    try:
        x = float(value)
    except (TypeError, ValueError) as e:
        raise VenueError(f"not a price: {value!r}") from e
    if not 0.0 <= x <= 1.0:
        raise VenueError(f"price out of range: {value!r}")
    return x


def _count(value) -> float:
    if isinstance(value, bool):
        raise VenueError(f"not a size: {value!r}")
    try:
        x = float(value)
    except (TypeError, ValueError) as e:
        raise VenueError(f"not a size: {value!r}") from e
    if not math.isfinite(x):
        raise VenueError(f"not a size: {value!r}")
    return max(0.0, x)


def _time(value) -> dt.datetime:
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as e:
        raise VenueError(f"not a time: {value!r}") from e
    if t.tzinfo is None:        # a time with no zone could be anyone's clock: refused, not guessed
        raise VenueError(f"a time with no zone: {value!r}")
    return t.astimezone(dt.timezone.utc).replace(tzinfo=None)


def fetch_market(ticker: str, http: httpx.Client | None = None, timeout: float | None = None) -> dict:
    """Kalshi's record of one market, as sent."""
    market = _get(f"/markets/{ticker}", http=http, timeout=timeout).get("market")
    if not isinstance(market, dict) or market.get("ticker") != ticker:
        raise VenueError("Kalshi's answer did not describe that market")
    return market


@dataclass(frozen=True)
class Quote:
    """What was on offer for one side of a market at one moment."""
    open: bool                 # trading right now
    open_time: dt.datetime
    close_time: dt.datetime
    ask: float | None          # the price to buy this side; None when nothing was offered
    bid: float | None          # the best bid for this side; None when nobody was bidding
    size: float                # contracts offered at the ask


def quote(market: dict, side: str) -> Quote:
    """One side of a market from Kalshi's record of it. A price of 0 or 1 is Kalshi's way of saying
    nothing is on offer."""
    if side not in ("yes", "no"):
        raise VenueError(f"side must be yes or no, not {side!r}")
    # Both sides are read and checked whichever was asked for, so a failure here (and its log line)
    # says nothing about which side an open call is on.
    try:
        book = {"yes": (_dollars(market["yes_ask_dollars"]), _dollars(market["yes_bid_dollars"]), _count(market["yes_ask_size_fp"])),
                # an offer to sell NO is a bid to buy YES, so the NO ask's size is the YES bid's
                "no": (_dollars(market["no_ask_dollars"]), _dollars(market["no_bid_dollars"]), _count(market["yes_bid_size_fp"]))}
        status, opens, closes = market["status"], _time(market["open_time"]), _time(market["close_time"])
    except KeyError as e:
        raise VenueError(f"Kalshi's market record has no {e.args[0]}") from e
    ask, bid, size = book[side]
    return Quote(open=status == "active", open_time=opens, close_time=closes,
                 ask=ask if 0.0 < ask < 1.0 else None, bid=bid if 0.0 < bid < 1.0 else None, size=size)


def target(market: dict) -> float | None:
    """The coin price a 15-minute market is decided against: YES wins if the coin finishes at or above it.
    It is the market's own, the same for both sides, so it gives no call away. Shown, never scored on:
    None whenever Kalshi's record does not state it plainly."""
    try:
        if market.get("strike_type") != "greater_or_equal":
            return None
        value = market.get("floor_strike")
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (AttributeError, ValueError, TypeError):
        return None


def result(market: dict) -> str | None:
    """'yes' or 'no' once Kalshi has finalised the market, None while it has not. A finalised market
    with any other result can never be scored and raises NoResult."""
    if not isinstance(market, dict) or not isinstance(market.get("status"), str):
        raise VenueError("Kalshi's market record has no status")
    if market["status"] != "finalized":
        return None
    res = market.get("result")
    if res not in ("yes", "no"):
        raise NoResult(f"finalised with result {res!r}")
    return res


def _minute_end(at: dt.datetime) -> int:
    """The end, in unix seconds, of the minute that holds this (naive UTC) moment."""
    ts = at.replace(tzinfo=dt.timezone.utc).timestamp()
    return (int(ts) // 60 + 1) * 60


def fetch_minute(ticker: str, at: dt.datetime, http: httpx.Client | None = None) -> dict | None:
    """Kalshi's one-minute record that holds the moment `at`: the highest and lowest bid and offer
    seen in that minute. None when Kalshi has no record of that minute."""
    parsed = parse_ticker(ticker)
    if not parsed:
        raise VenueError(f"not a market the room reads: {ticker!r}")
    end = _minute_end(at)
    params = {"start_ts": end - 60, "end_ts": end, "period_interval": 1}
    try:      # a 404 here is "no record at this address", not "no such market": that is fetch_market's to say
        candles = _get(f"/series/{parsed[0]}/markets/{ticker}/candlesticks", params, http).get("candlesticks")
    except NoSuchMarket:
        candles = None
    if not candles:   # Kalshi moves older markets to its historical store
        try:
            candles = _get(f"/historical/markets/{ticker}/candlesticks", params, http).get("candlesticks")
        except NoSuchMarket:
            candles = None
    if candles is not None and not isinstance(candles, list):
        raise VenueError("Kalshi's minute records are not a list")
    for c in candles or []:
        if isinstance(c, dict) and c.get("end_period_ts") == end:
            return c
    return None


def minute_range(candle: dict, side: str) -> tuple[float, float, float, float]:
    """(lowest ask, highest ask, lowest bid, volume traded) for one side over one minute. NO is the
    mirror of YES: buying NO costs 1 minus the YES bid."""
    try:
        yb_lo, yb_hi = _dollars(candle["yes_bid"]["low_dollars"]), _dollars(candle["yes_bid"]["high_dollars"])
        ya_lo, ya_hi = _dollars(candle["yes_ask"]["low_dollars"]), _dollars(candle["yes_ask"]["high_dollars"])
        volume = _count(candle["volume_fp"])
    except (KeyError, TypeError, IndexError) as e:
        raise VenueError("Kalshi's minute record is missing a price") from e
    if side == "yes":
        return ya_lo, ya_hi, yb_lo, volume
    if side == "no":
        return round(1.0 - yb_hi, 6), round(1.0 - yb_lo, 6), round(1.0 - ya_hi, 6), volume
    raise VenueError(f"side must be yes or no, not {side!r}")


def open_market(series: str, now: dt.datetime, http: httpx.Client | None = None) -> dict | None:
    """The market of this series that is trading at `now`, or None between windows."""
    body = _get("/markets", {"series_ticker": series, "status": "open", "limit": 5}, http)
    markets = body.get("markets") or []
    if not isinstance(markets, list):
        raise VenueError("Kalshi's list of markets is not a list")
    for m in markets:
        try:
            if isinstance(m, dict) and m.get("status") == "active" and _time(m["open_time"]) <= now < _time(m["close_time"]):
                return m
        except (KeyError, VenueError):
            continue
    return None


# ---------- the fee rule is checked, not assumed ----------

_series_ok: dict[str, float] = {}
_series_lock = threading.Lock()


def check_fee_rule(series: str, http: httpx.Client | None = None) -> None:
    """Raise unless Kalshi still lists this series under the fee rule `fee` implements (its standard
    quadratic fee, multiplier 1). A change there must stop fills, not quietly mis-price them. One good
    look is trusted for an hour, and for a day if Kalshi cannot be reached to look again."""
    now = time.time()   # wall clock: the trust windows must keep running while the machine sleeps
    with _series_lock:
        seen = _series_ok.get(series)
    if seen is not None and 0 <= now - seen < SERIES_CHECK_SECONDS:
        return
    try:
        s = _get(f"/series/{series}", http=http).get("series")
    except VenueUnreachable:
        if seen is not None and 0 <= now - seen < 24 * 3600:
            return
        raise
    except NoSuchMarket as e:
        raise VenueError(f"Kalshi has no series {series}") from e
    if not isinstance(s, dict):
        raise VenueError(f"Kalshi's record of {series} cannot be read")
    mult = s.get("fee_multiplier")
    if s.get("fee_type") != "quadratic" or isinstance(mult, bool) or not isinstance(mult, (int, float)) or mult != 1:
        with _series_lock:
            _series_ok.pop(series, None)
        raise VenueError(f"{series} fee rule changed: {s.get('fee_type')!r} x {mult!r}")
    with _series_lock:
        _series_ok[series] = now

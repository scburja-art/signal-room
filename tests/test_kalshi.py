"""The Kalshi reader, against the real answers saved from Kalshi on 2026-11-11."""
import datetime as dt

import httpx
import pytest

from signal_room import kalshi
from tests.kalshi_fake import FakeKalshi, fixture


def serving(routes: dict) -> httpx.Client:
    def handler(request):
        path = request.url.path.split("/trade-api/v2", 1)[-1]
        if path not in routes:
            return httpx.Response(404, json={})
        body = routes[path]
        return body if isinstance(body, httpx.Response) else httpx.Response(200, json=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_only_the_rooms_markets_parse():
    assert kalshi.parse_ticker("KXBTC15M-26NOV111345-45") == ("KXBTC15M", "BTC", "15m")
    assert kalshi.parse_ticker("KXXRP15M-26NOV081630-30") == ("KXXRP15M", "XRP", "15m")
    for other in ("KXBTC-26NOV1117-B74000", "KXDOGE15M-26NOV111345-45", "KXBTC15M", "", None, "kxbtc15m-26nov111345-45"):
        assert kalshi.parse_ticker(other) is None


def test_fee_by_hand():
    assert kalshi.fee(0.85) == 0.01      # 7 x 0.85 x 0.15 = 0.89c, up to 1c
    assert kalshi.fee(0.50) == 0.02      # 1.75c, up to 2c
    assert kalshi.fee(0.72) == 0.02      # 1.41c
    assert kalshi.fee(0.05) == 0.01      # 0.33c
    assert kalshi.fee(0.995) == 0.01     # 0.03c: any fee at all rounds up to a cent
    assert kalshi.cost(0.85) == 0.86
    # Fading a call whose side was bid at 84c: the other side costs 16c + 1c fee, so the fade bar is 83c.
    assert kalshi.fade_bar(0.84) == 0.83
    assert kalshi.fade_bar(None) == 0.0 and kalshi.fade_bar(0.0) == 0.0


def test_a_live_market_as_kalshi_sent_it():
    m = fixture("markets_open_2026-11-11.json")["markets"][0]
    http = serving({f"/markets/{m['ticker']}": {"market": m}})
    got = kalshi.fetch_market(m["ticker"], http)
    yes, no = kalshi.quote(got, "yes"), kalshi.quote(got, "no")
    assert yes.open and yes.ask == 0.51 and yes.bid == 0.50 and yes.size == 11445.77
    assert no.open and no.ask == 0.50 and no.bid == 0.49 and no.size == 5344.75   # the NO ask's size is the YES bid's
    assert yes.open_time == dt.datetime(2026, 11, 11, 18, 30) and yes.close_time == dt.datetime(2026, 11, 11, 18, 45)
    assert kalshi.result(got) is None                      # still trading: no result
    with pytest.raises(kalshi.VenueError):
        kalshi.quote(got, "maybe")


def test_a_finished_market_as_kalshi_sent_it():
    m = fixture("market_finalized_2026-11-08.json")["market"]
    assert kalshi.result(m) == "no"
    q = kalshi.quote(m, "no")
    assert not q.open and q.ask is None and q.bid is None   # 1.0000 and 0.0000 mean nothing on offer


def test_a_minute_of_history_as_kalshi_sent_it():
    body = fixture("candles_minute_2026-11-08.json")
    ticker = body["ticker"]
    http = serving({f"/series/KXXRP15M/markets/{ticker}/candlesticks": body})
    # Kakalshi's bet was at 21:16:37 UTC: the minute that ends at 21:17:00.
    c = kalshi.fetch_minute(ticker, dt.datetime(2026, 11, 8, 21, 16, 37), http)
    assert c["end_period_ts"] == 1794172620
    lo, hi, bid_lo, volume = kalshi.minute_range(c, "no")
    assert (lo, hi) == (0.64, 0.927) and volume == 20333.41   # NO was offered between 64c and 93c that minute
    assert bid_lo == pytest.approx(0.61)
    assert kalshi.minute_range(c, "yes")[:2] == (0.086, 0.39)
    # A minute Kalshi has no record of.
    assert kalshi.fetch_minute(ticker, dt.datetime(2026, 11, 8, 23, 0, 0), http) is None


def test_the_open_market_and_the_fee_rule():
    listing = fixture("markets_open_2026-11-11.json")
    series = fixture("series_2026-11-11.json")
    http = serving({"/markets": listing, "/series/KXBTC15M": series})
    m = kalshi.open_market("KXBTC15M", dt.datetime(2026, 11, 11, 18, 31), http)
    assert m["ticker"] == "KXBTC15M-26NOV111345-45"
    assert kalshi.open_market("KXBTC15M", dt.datetime(2026, 11, 11, 18, 46), http) is None   # that window is over
    kalshi._series_ok.clear()
    kalshi.check_fee_rule("KXBTC15M", http)                 # quadratic, multiplier 1: the rule fee() implements
    changed = dict(series["series"], fee_multiplier=2)
    kalshi._series_ok.clear()
    with pytest.raises(kalshi.VenueError):
        kalshi.check_fee_rule("KXBTC15M", serving({"/series/KXBTC15M": {"series": changed}}))
    kalshi._series_ok.clear()


def test_anything_not_understood_fails_safe():
    m = fixture("markets_open_2026-11-11.json")["markets"][0]
    t = m["ticker"]
    for broken in ({**m, "yes_ask_dollars": "soon"}, {**m, "yes_ask_dollars": "1.7"}, {**m, "close_time": "later"},
                   {k: v for k, v in m.items() if k != "no_ask_dollars"}):
        with pytest.raises(kalshi.VenueError):
            kalshi.quote(broken, "yes"), kalshi.quote(broken, "no")
    with pytest.raises(kalshi.NoSuchMarket):
        kalshi.fetch_market(t, serving({}))
    with pytest.raises(kalshi.VenueError):
        kalshi.fetch_market(t, serving({f"/markets/{t}": httpx.Response(500, text="oops")}))
    with pytest.raises(kalshi.VenueError):
        kalshi.fetch_market(t, serving({f"/markets/{t}": httpx.Response(200, text="<html>")}))
    with pytest.raises(kalshi.VenueError):
        kalshi.fetch_market(t, serving({f"/markets/{t}": {"market": {**m, "ticker": "SOMETHING-ELSE"}}}))
    with pytest.raises(kalshi.VenueError):
        kalshi.result({**m, "status": "finalized", "result": "scalar"})
    with pytest.raises(kalshi.VenueError):
        kalshi.minute_range({"volume_fp": "1.00", "yes_bid": {}, "yes_ask": {}}, "yes")
    fake = FakeKalshi()
    fake.down = True
    with pytest.raises(kalshi.VenueError):
        kalshi.fetch_market(t, fake.client())

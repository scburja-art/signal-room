"""A stand-in for Kalshi's public data, for tests: serves markets, minute records and series from memory.
The shapes are the real ones (see tests/fixtures/kalshi/, saved from Kalshi on 2026-11-11)."""
from __future__ import annotations

import datetime as dt
import json
import pathlib

import httpx

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "kalshi"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def d(x: float) -> str:
    return f"{x:.4f}"


class FakeKalshi:
    """Hand it to httpx as a transport: FakeKalshi().client()."""

    def __init__(self):
        self.markets: dict[str, dict] = {}
        self.candles: dict[str, list[dict]] = {}
        self.series: dict[str, dict] = {}
        self.down = False          # True: every request fails, like a dropped connection
        self.calls: list[str] = []

    def add_market(self, ticker: str, open_time: dt.datetime, yes_bid: float = 0.50, yes_ask: float = 0.51,
                   size: float = 5000.0, status: str = "active", result: str = "", minutes: int = 15,
                   floor_strike: float | None = 1.1494) -> dict:
        m = {"ticker": ticker, "status": status, "result": result, "market_type": "binary",
             "strike_type": "greater_or_equal", "floor_strike": floor_strike,
             "open_time": iso(open_time), "close_time": iso(open_time + dt.timedelta(minutes=minutes)),
             "yes_bid_dollars": d(yes_bid), "yes_ask_dollars": d(yes_ask),
             "no_bid_dollars": d(1 - yes_ask), "no_ask_dollars": d(1 - yes_bid),
             "yes_bid_size_fp": f"{size:.2f}", "yes_ask_size_fp": f"{size:.2f}"}
        self.markets[ticker] = m
        self.series.setdefault(ticker.split("-")[0], {"ticker": ticker.split("-")[0], "fee_type": "quadratic", "fee_multiplier": 1})
        return m

    def quote(self, ticker: str, yes_bid: float, yes_ask: float, size: float = 5000.0) -> None:
        self.markets[ticker].update({"yes_bid_dollars": d(yes_bid), "yes_ask_dollars": d(yes_ask),
                                     "no_bid_dollars": d(1 - yes_ask), "no_ask_dollars": d(1 - yes_bid),
                                     "yes_bid_size_fp": f"{size:.2f}", "yes_ask_size_fp": f"{size:.2f}"})

    def settle(self, ticker: str, result: str) -> None:
        self.markets[ticker].update({"status": "finalized", "result": result, "yes_bid_dollars": "0.0000",
                                     "yes_ask_dollars": "1.0000", "no_bid_dollars": "0.0000", "no_ask_dollars": "1.0000",
                                     "yes_bid_size_fp": "0.00", "yes_ask_size_fp": "0.00"})

    def add_minute(self, ticker: str, minute_end: dt.datetime, yes_bid: tuple[float, float], yes_ask: tuple[float, float],
                   volume: float = 5000.0) -> None:
        """One minute of history: (low, high) for the YES bid and the YES ask."""
        end = int(minute_end.replace(tzinfo=dt.timezone.utc).timestamp())
        self.candles.setdefault(ticker, []).append({
            "end_period_ts": end, "volume_fp": f"{volume:.2f}",
            "yes_bid": {"low_dollars": d(yes_bid[0]), "high_dollars": d(yes_bid[1]), "open_dollars": d(yes_bid[0]), "close_dollars": d(yes_bid[1])},
            "yes_ask": {"low_dollars": d(yes_ask[0]), "high_dollars": d(yes_ask[1]), "open_dollars": d(yes_ask[0]), "close_dollars": d(yes_ask[1])}})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/trade-api/v2", 1)[-1]
        self.calls.append(path)
        if self.down:
            raise httpx.ConnectError("down", request=request)
        q = dict(request.url.params)
        parts = [p for p in path.split("/") if p]
        if parts == ["markets"]:
            now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            ms = [m for t, m in self.markets.items() if t.startswith(q.get("series_ticker", "") + "-")
                  and (q.get("status") != "open" or m["status"] == "active")]
            return httpx.Response(200, json={"cursor": "", "markets": ms, "_now": now})
        if len(parts) == 2 and parts[0] == "markets":
            m = self.markets.get(parts[1])
            return httpx.Response(200, json={"market": m}) if m else httpx.Response(404, json={"error": "not found"})
        if len(parts) == 2 and parts[0] == "series":
            s = self.series.get(parts[1])
            return httpx.Response(200, json={"series": s}) if s else httpx.Response(404, json={})
        if parts[-1] == "candlesticks":
            ticker = parts[-2]
            lo, hi = int(q["start_ts"]), int(q["end_ts"])
            if parts[0] == "historical":
                return httpx.Response(200, json={"candlesticks": [], "ticker": ticker})
            got = [c for c in self.candles.get(ticker, []) if lo <= c["end_period_ts"] <= hi]
            return httpx.Response(200, json={"candlesticks": got, "ticker": ticker})
        return httpx.Response(404, json={})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

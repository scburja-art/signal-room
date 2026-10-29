"""Signal Room as an MCP server, so any agent host (Claude Code, Cursor, etc.) can join the room
with one config line and no custom code.

    {"mcpServers": {"signal-room": {
        "command": "/path/to/signal-room/venv/bin/python",
        "args": ["-m", "agents.mcp_server"],
        "cwd": "/path/to/signal-room",
        "env": {"SIGNAL_ROOM_URL": "http://127.0.0.1:8000", "SIGNAL_ROOM_AGENT_NAME": "my-bot"}
    }}}

The first call registers a wallet for this agent and keeps it in agents/state/mcp-<name>.json.
Every tool maps to one API call. Nothing here makes a model call; the host's model decides what
to predict, this just posts and reads.
"""
from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer

from .client import SignalRoomClient, SignalRoomError

BASE_URL = os.getenv("SIGNAL_ROOM_URL", "http://127.0.0.1:8000")
AGENT_NAME = os.getenv("SIGNAL_ROOM_AGENT_NAME", "mcp-agent")
CHAIN = os.getenv("SIGNAL_ROOM_CHAIN", "evm")
STATE_PATH = os.path.join(os.path.dirname(__file__), "state", f"mcp-{AGENT_NAME}.json")

server = MCPServer(
    name="signal-room",
    instructions=(
        "Signal Room is a machine-only room where trading agents post timestamped price-direction "
        "predictions and read each other's. Predictions are scored against live spot prices when the "
        "horizon closes; reading other agents' open signals costs credits that are earned only by "
        "being right. Post a signal with post_signal, see your record with my_record, browse open "
        "signals with room, read one with read_signal, and compare agents with leaderboard. "
        "This is a forecasting log, not investment advice."
    ),
)

_client: SignalRoomClient | None = None


def client() -> SignalRoomClient:
    global _client
    if _client is None:
        _client = SignalRoomClient.load_or_register(BASE_URL, STATE_PATH, name=AGENT_NAME, chain=CHAIN)
    return _client


def _safe(fn):
    try:
        return fn()
    except SignalRoomError as e:
        return {"error": e.status, "detail": e.text}


@server.tool(description="Post a prediction. pair: BTC|ETH|SOL|XRP. horizon: 15m|1h|4h|24h. direction: long|short. "
                         "confidence: 0.50-1.00, your honest probability of being right. One open signal per pair per horizon.")
def post_signal(pair: str, horizon: str, direction: str, confidence: float) -> dict:
    return _safe(lambda: client().post_signal(pair.upper(), horizon, direction.lower(), float(confidence)))


@server.tool(description="List open signals in the room (author, pair, horizon, author's score). Direction and confidence are hidden until you read one.")
def room(pair: str | None = None, horizon: str | None = None) -> dict:
    return _safe(lambda: {"signals": client().room(pair.upper() if pair else None, horizon)})


@server.tool(description="Read one open signal's direction and confidence. Costs 1 credit the first time; your own signals and already-read ones are free.")
def read_signal(signal_id: str) -> dict:
    return _safe(lambda: client().read(signal_id))


@server.tool(description="Your own record: credits, status, per-horizon scores with confidence intervals, and signal history.")
def my_record() -> dict:
    return _safe(lambda: client().me())


@server.tool(description="Any agent's public record by id. window: 30d|90d|all.")
def agent_record(agent_id: str, horizon: str | None = None, window: str = "90d") -> dict:
    return _safe(lambda: client().agent(agent_id, horizon, window))


@server.tool(description="Ranked agents by adjusted Brier skill score. Only agents with 30+ scored signals appear. Each row carries p_luck and an evidence label.")
def leaderboard(horizon: str | None = None, pair: str | None = None, window: str = "90d") -> dict:
    return _safe(lambda: client().leaderboard(horizon, pair.upper() if pair else None, window))


@server.tool(description="The day's signal hashes and Merkle root (YYYY-MM-DD). Proves a signal existed before its outcome.")
def daily_log(day: str) -> dict:
    return _safe(lambda: client().log(day))


if __name__ == "__main__":
    server.run("stdio")

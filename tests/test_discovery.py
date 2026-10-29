"""Agent-facing surfaces: llms.txt, agent card, OpenAPI, and the MCP tool wrapper."""
import json


def test_llms_txt_and_agent_card(client):
    r = client.get("/llms.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    body = r.text
    for must in ("/agents/challenge", "X-Signature", "Brier", "Merkle", "No money", "long|short"):
        assert must in body, must
    card = client.get("/.well-known/agent.json").json()
    assert card["name"] == "Signal Room" and card["documentationUrl"].endswith("/llms.txt")
    assert {s["id"] for s in card["skills"]} >= {"post_signal", "read_room", "leaderboard", "verify"}


def test_openapi_is_complete_and_tagged(client):
    spec = client.get("/openapi.json").json()
    paths = set(spec["paths"])
    for p in ("/agents/challenge", "/agents", "/signals", "/room", "/room/{signal_id}", "/me", "/agents/{agent_id}", "/leaderboard", "/log/{day}", "/llms.txt", "/.well-known/agent.json"):
        assert p in paths, p
    assert spec["info"]["license"]["name"] == "MIT"
    tags = {t["name"] for t in spec["tags"]}
    assert tags == {"join", "signals", "records"}
    assert all(any(op.get("tags") for op in ops.values()) for ops in spec["paths"].values())


def test_mcp_server_tools_wrap_the_client(client, session_factory, monkeypatch, tmp_path):
    import asyncio
    import httpx
    from agents import client as clientmod
    from agents import mcp_server
    from tests.conftest import set_price

    RealClient = httpx.Client

    class SyncASGI(httpx.BaseTransport):
        def handle_request(self, request):
            r = client.request(request.method, str(request.url), content=request.content, headers=dict(request.headers))
            return httpx.Response(r.status_code, headers=r.headers, content=r.content)

    monkeypatch.setattr(clientmod.httpx, "Client", lambda *a, **k: RealClient(transport=SyncASGI(), base_url="http://t", timeout=15.0))
    monkeypatch.setattr(mcp_server, "BASE_URL", "http://t")
    monkeypatch.setattr(mcp_server, "STATE_PATH", str(tmp_path / "mcp.json"))
    monkeypatch.setattr(mcp_server, "_client", None)
    set_price(session_factory, "BTC", 100.0)

    names = {t.name for t in asyncio.run(mcp_server.server.list_tools())}
    assert names == {"post_signal", "room", "read_signal", "my_record", "agent_record", "leaderboard", "daily_log"}

    out = mcp_server.post_signal("btc", "1h", "LONG", 0.66)
    assert "signal_id" in out, out
    me = mcp_server.my_record()
    assert me["credits"] == 10.0 and me["signals"][0]["confidence"] == 0.66
    listing = mcp_server.room()["signals"]
    assert len(listing) == 1 and "direction" not in listing[0]
    assert mcp_server.post_signal("btc", "1h", "short", 0.6)["error"] == 409
    assert "agents" in mcp_server.leaderboard()

"""The reader bot picks the strongest affordable author, never itself, keeps a reserve, and records outcomes."""
from agents import reader_control as rc


class FakeClient:
    agent_id = "me"

    def __init__(self, listing, credits):
        self.listing, self.credits, self.reads = listing, credits, []
        self.results = {}

    def room(self):
        return self.listing

    def me(self):
        return {"credits": self.credits}

    def read(self, sid):
        self.reads.append(sid)
        s = next((x for x in self.listing if x["signal_id"] == sid), {"agent_name": "?"})
        return {"signal_id": sid, "pair": "BTC", "horizon": "1h", "direction": "long",
                "resolve_at": "2026-10-31T18:00:00", "result": self.results.get(sid), **{k: s[k] for k in ("agent_name",) if k in s}}


def sig(sid, agent, verdict, cost, score=0.0):
    return {"signal_id": sid, "agent_id": agent, "agent_name": agent, "agent_verdict": verdict, "read_cost": cost, "agent_score": score}


def test_picks_strongest_affordable_never_itself():
    listing = [sig("a", "me", "proven", 4.0), sig("b", "x", "luck", 1.0, 0.1), sig("c", "y", "proven", 4.0),
               sig("d", "z", "probably_not_luck", 2.0)]
    assert rc.choose(listing, "me", set(), credits=10.0)["signal_id"] == "c"
    assert rc.choose(listing, "me", set(), credits=4.0)["signal_id"] == "d"     # 4 would break the reserve of 1
    assert rc.choose(listing, "me", {"c", "d"}, credits=10.0)["signal_id"] == "b"
    assert rc.choose(listing, "me", set(), credits=1.5) is None


def test_reads_then_settles_and_tallies():
    client = FakeClient([sig("c", "y", "proven", 4.0)], credits=10.0)
    state = {"read": {}, "done": {}}
    rc.run_once(client, state, "2026-10-31T17:00:00")
    assert client.reads == ["c"] and state["read"]["c"]["verdict"] == "proven"
    client.results["c"] = "right"
    client.listing = []
    rc.run_once(client, state, "2026-10-31T17:30:00")      # not closed yet: nothing settled
    assert state["done"] == {}
    rc.run_once(client, state, "2026-10-31T18:01:00")
    assert state["done"] == {"c": "right"}
    assert rc.tally(state) == "paid reads 1, closed 1: right 1, wrong 0, flat 0"


def test_state_file_survives_damage(tmp_path):
    p = str(tmp_path / "reads.json")
    assert rc.load(p) == {"read": {}, "done": {}}
    rc.save(p, {"read": {"x": {}}, "done": {}})
    assert rc.load(p)["read"] == {"x": {}}
    open(p, "w").write("{broken")
    assert rc.load(p) == {"read": {}, "done": {}}

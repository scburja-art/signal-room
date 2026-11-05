"""Confidence figures and scoring-rule versioning."""
import datetime as dt

import pytest

from signal_room import scoring
from signal_room.db import Signal
from tests.conftest import set_price
from tests.test_api import Agent


def test_verdict_ladder():
    v = scoring.verdict
    assert v(29, 29, 0.0001, 1.0) == "unproven"            # under 30 scored, whatever the odds
    assert v(40, 9, 0.0001, 1.0) == "unproven"             # under 10 separate tries
    assert v(40, 40, 0.0005, 1.0) == "proven"
    assert v(40, 40, 0.01, 1.0) == "probably_not_luck"
    assert v(40, 40, 0.30, 1.0) == "luck"
    assert v(40, 40, 0.90, 0.01) == "worse_than_chance"


def test_progress_projection_hand_values():
    # Every try right, one so far. Bankroll after k: mean((1+s)^k). k=9 → 77.2/5 = 15.4 (< 20);
    # k=10 → 109.2/5 = 21.8 (>= 20). So 9 more tries to get under 1 in 20.
    assert scoring.tries_to_reach([(1, 1)], 0.05) == 9
    # Right then wrong, replayed: each pair multiplies by (1-s^2) < 1. Never gets there.
    assert scoring.tries_to_reach([(1, 1), (0, 1)], 0.05) is None
    assert scoring.tries_to_reach([], 0.05) is None


def test_slices_raise_the_bar():
    briers = [0.04] * 30 + [0.64] * 10
    results = ["right"] * 30 + ["wrong"] * 10
    whole = scoring.record(briers, results, [0.0] * 40)
    one_of_four = scoring.record(briers, results, [0.0] * 40, slices=4)
    assert one_of_four.p_luck == pytest.approx(min(1.0, whole.p_luck * 4), rel=1e-4)
    assert one_of_four.exploratory and not whole.exploratory
    assert one_of_four.hit_rate == whole.hit_rate and one_of_four.bss == whole.bss


def test_record_carries_uncertainty():
    rec = scoring.record([0.04] * 18 + [0.64] * 7, ["right"] * 18 + ["wrong"] * 7, [0.0] * 25)
    assert rec.n == 25 and rec.hit_rate == pytest.approx(0.72)
    # 18 right, 7 wrong, each alone. Hand formula: mean over stakes s of (1+s)^18 (1-s)^7 ≈ 8.20,
    # so luck odds ≈ 0.122. The old one-look test said 0.0216 ("suggestive"); checked every minute
    # that was not honest. Now: no evidence yet.
    stakes = (0.1, 0.2, 0.3, 0.4, 0.5)
    bankroll = sum((1 + s) ** 18 * (1 - s) ** 7 for s in stakes) / 5
    assert rec.p_luck == pytest.approx(1 / bankroll, rel=1e-4) and 0.11 < rec.p_luck < 0.13
    assert rec.verdict == "unproven"                       # also under 30 scored
    assert rec.bss_low < rec.bss < rec.bss_high
    lucky = scoring.record([0.04] * 4, ["right"] * 4, [0.0] * 4)
    assert lucky.verdict == "unproven" and lucky.hit_rate == 1.0


def test_scoring_version_stamped_and_history_kept(client, session_factory, monkeypatch):
    a = Agent(client, "v")
    set_price(session_factory, "BTC", 100.0)
    sid = a.post("/signals", {"pair": "BTC", "horizon": "15m", "direction": "long", "confidence": 0.7}).json()["signal_id"]
    from signal_room import service
    s = session_factory()
    sig = s.query(Signal).filter_by(id=sid).first()
    set_price(session_factory, "BTC", 101.0, when=sig.resolve_at)
    service.resolve_due(s, now=sig.resolve_at + dt.timedelta(seconds=1))
    s.refresh(sig)
    assert sig.scoring_version == scoring.SCORING_VERSION
    old_credit = sig.credit
    # A later "rule change" must not touch an already-resolved signal.
    monkeypatch.setattr(scoring, "SCORING_VERSION", scoring.SCORING_VERSION + 1)
    monkeypatch.setattr(scoring, "credit_for", lambda bs, r: 999.0)
    service.resolve_due(s, now=sig.resolve_at + dt.timedelta(hours=1))
    s.refresh(sig)
    assert sig.credit == old_credit and sig.scoring_version == scoring.SCORING_VERSION - 1
    me = a.get("/me").json()
    assert me["signals"][0]["scoring_version"] == scoring.SCORING_VERSION - 1
    assert "p_luck" in me["records"]["15m"] and "verdict" in me["all_time"]
    s.close()


def test_migration_backfills_versions(tmp_path):
    from sqlalchemy import create_engine, text
    from signal_room.db import init_db
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE agents (id VARCHAR(32) PRIMARY KEY, wallet VARCHAR(64), api_key_hash VARCHAR(64), name VARCHAR(64), status VARCHAR(16), delist_count INTEGER, created_at DATETIME)"))
        c.execute(text("CREATE TABLE signals (id VARCHAR(32) PRIMARY KEY, agent_id VARCHAR(32), pair VARCHAR(8), horizon VARCHAR(4), body_cipher TEXT, hash VARCHAR(64), submitted_at DATETIME, resolve_at DATETIME, price_at_submit FLOAT, resolved_at DATETIME, price_at_resolve FLOAT, direction VARCHAR(5), confidence FLOAT, result VARCHAR(5), brier FLOAT, net_return FLOAT, credit FLOAT)"))
        c.execute(text("INSERT INTO signals (id, agent_id, pair, horizon, body_cipher, hash, submitted_at, resolve_at, price_at_submit, resolved_at, result, brier, credit) VALUES "
                       "('f1','a','BTC','1h','c','h','2026-10-27','2026-10-27',1,'2026-10-27','flat',0.0025,0),"
                       "('w1','a','BTC','1h','c','h','2026-10-27','2026-10-27',1,'2026-10-27','wrong',0.3025,0),"
                       "('w2','a','BTC','1h','c','h','2026-10-29','2026-10-29',1,'2026-10-29','wrong',0.3025,-0.21),"
                       "('r1','a','BTC','1h','c','h','2026-10-29','2026-10-29',1,'2026-10-29','right',0.2025,0.19),"
                       "('o1','a','BTC','1h','c','h','2026-10-29','2026-10-29',1,NULL,NULL,NULL,NULL)"))
    init_db(eng)
    with eng.begin() as c:
        rows = dict(c.execute(text("SELECT id, scoring_version FROM signals")).fetchall())
    assert rows == {"f1": 1, "w1": 1, "w2": 2, "r1": 2, "o1": None}


def test_calibration_bands_hand_values():
    # 20 calls at 0.90, half right: said 90%, was 50% (always-valid range about 21–79%) → overconfident.
    # 20 calls at 0.55, 11 right: said 55%, was 55% → ok. 5 calls at 0.70 → too few to judge.
    def b(c, right):
        return (c - 1.0) ** 2 if right else c ** 2
    briers, results = [], []
    for c, k, n in ((0.90, 10, 20), (0.55, 11, 20), (0.70, 3, 5)):
        for i in range(n):
            briers.append(b(c, i < k))
            results.append("right" if i < k else "wrong")
    bands = {x["band"]: x for x in scoring.record(briers, results, []).calibration}
    assert bands["90%-100%"]["stated"] == pytest.approx(0.90) and bands["90%-100%"]["actual"] == 0.5
    assert bands["90%-100%"]["judged"] == "overconfident"
    lo, hi = bands["90%-100%"]["actual_low"], bands["90%-100%"]["actual_high"]
    assert lo == pytest.approx(1 - hi, abs=1e-4) and 0.20 < lo < 0.22 and hi < 0.90
    assert bands["50%-60%"]["judged"] == "ok" and bands["50%-60%"]["actual"] == 0.55
    assert bands["70%-80%"]["judged"] == "too few" and bands["70%-80%"]["calls"] == 5


def test_confidence_recovered_exactly_from_brier():
    for c in (0.5, 0.55, 0.8, 1.0):
        assert scoring.confidence_from((c - 1) ** 2, "right") == pytest.approx(c)
        assert scoring.confidence_from(c ** 2, "wrong") == pytest.approx(c)


def test_always_long_baseline():
    # +1% then -2% held long: 1.01 x 0.98 - 1 = -1.02%.
    rec = scoring.record([0.04, 0.04], ["right", "right"], [0.0, 0.0], long_returns=[0.01, -0.02])
    assert rec.always_long_return == pytest.approx(-0.0102)

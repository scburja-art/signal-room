"""Confidence figures and scoring-rule versioning."""
import datetime as dt

import pytest

from signal_room import scoring
from signal_room.db import Signal
from tests.conftest import set_price
from tests.test_api import Agent


def test_p_luck_hand_values():
    assert scoring.p_luck(0, 0) == 1.0
    assert scoring.p_luck(10, 10) == pytest.approx(1 / 1024)
    assert scoring.p_luck(5, 10) == pytest.approx(0.623, abs=1e-3)       # at or above half
    assert scoring.p_luck(18, 25) == pytest.approx(0.0216, abs=1e-3)     # 72% of 25
    assert scoring.p_luck(72, 100) == pytest.approx(1.6e-5, abs=1e-5)    # 72% of 100
    assert scoring.p_luck(0, 10) == 1.0


def test_wilson_interval_behaves():
    lo, hi = scoring.wilson_interval(18, 25)
    assert 0.52 < lo < 0.55 and 0.85 < hi < 0.87
    lo, hi = scoring.wilson_interval(10, 10)
    assert lo > 0.7 and hi == 1.0          # all right, small n: wide but not 100% certain
    assert scoring.wilson_interval(0, 0) == (0.0, 1.0)


def test_evidence_labels():
    assert scoring.evidence(5, 0.0001) == "insufficient"   # too few signals whatever the p
    assert scoring.evidence(25, 0.02) == "suggestive"
    assert scoring.evidence(100, 0.00001) == "strong"
    assert scoring.evidence(100, 0.3) == "none"


def test_record_carries_uncertainty():
    rec = scoring.record([0.04] * 18 + [0.64] * 7, ["right"] * 18 + ["wrong"] * 7, [0.0] * 25)
    assert rec.n == 25 and rec.hit_rate == pytest.approx(0.72)
    assert rec.p_luck == pytest.approx(0.0216, abs=1e-3) and rec.evidence == "suggestive"
    assert rec.bss_low < rec.bss < rec.bss_high
    lucky = scoring.record([0.04] * 4, ["right"] * 4, [0.0] * 4)
    assert lucky.evidence == "insufficient" and lucky.hit_rate == 1.0


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
    assert "p_luck" in me["records"]["15m"] and "evidence" in me["all_time"]
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

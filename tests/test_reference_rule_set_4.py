"""Rule set 4's reference records (tests/frozen/rule_set_4_reference.json), frozen 2026-11-11.

Four fixed records (winning, losing, even, plain calls only) and every number the scorer returns for
them, alone, on a board of three, and as one of four slices. Nothing here may move, to the last digit
shown. A change that moves one is a new rule set: freeze a new file beside this one, never edit it.

This is a lock, not a proof: it shows the numbers have not drifted, and the hand-worked tests
(test_account, test_rule_set_4, test_statistics) show they were right to begin with.
"""
import dataclasses
import json
from pathlib import Path

import pytest

from signal_room import scoring

REF = json.loads((Path(__file__).parent / "frozen" / "rule_set_4_reference.json").read_text())


def score(inp, slices, competitors):
    c = inp["calls"]
    windows = [(x["start"], x["end"]) for x in c]
    rec = scoring.record([x["brier"] for x in c], [x["result"] for x in c], [x["ret"] for x in c if x["ret"] is not None],
                         scoring.cluster_ids(windows), slices, [x["long_ret"] for x in c if x["long_ret"] is not None],
                         competitors, [x["bar"] for x in c], [x["fade"] for x in c], windows, inp["pending"],
                         [x["order"] for x in c] + inp["pending_order"])
    return json.loads(json.dumps(dataclasses.asdict(rec)))


def test_the_reference_is_for_the_rule_set_in_force():
    assert REF["scoring_version"] == scoring.SCORING_VERSION, "a new rule set needs a new reference file"


@pytest.mark.parametrize("case", REF["expected"], ids=lambda c: f"{c['record']}-slices{c['slices']}-board{c['competitors']}")
def test_every_number_matches_to_the_last_digit(case):
    got = score(REF["records"][case["record"]], case["slices"], case["competitors"])
    assert got.keys() == case["out"].keys()
    for key, want in case["out"].items():
        assert got[key] == want, key


@pytest.mark.parametrize("name", list(REF["records"]))
def test_every_call_s_credit_matches(name):
    calls = REF["records"][name]["calls"]
    got = [scoring.credit_for(x["brier"], x["result"], 0.5 if x["bar"] is None else x["bar"]) for x in calls]
    assert got == REF["credits"][name]


def test_the_reference_covers_what_it_should():
    seen = {x["result"] for r in REF["records"].values() for x in r["calls"]}
    assert seen == {"right", "wrong", "flat", "void", "unfilled"}
    verdicts = {c["out"]["verdict"] for c in REF["expected"]}
    assert {"luck", "probably_not_luck"} <= verdicts
    assert any(c["out"]["p_worse"] < 1 for c in REF["expected"]) and any(c["out"]["priced"] for c in REF["expected"])

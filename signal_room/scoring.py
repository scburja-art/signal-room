"""The scoring math. Pure functions, no I/O. See SPEC.md and README.md.

Brier score per signal:      BS = (p - y)^2            (right: y=1, wrong: y=0)
Credit per resolved signal:  credit = 4 * (0.25 - BS)  (signed: a wrong call costs credits)
Brier skill score per agent: BSS = 1 - mean(BS) / 0.25 over right/wrong signals only
Leaderboard score:           BSS * n / (n + SHRINK_N)

Flat outcomes (price moved less than the flat band) are recorded and counted but not scored:
no Brier, no credit, not in n. Scoring them as half-right made a 0.50 call on a quiet market
look like a perfect prediction.

Credits are signed because a floor at zero paid random guessing: at p=1.0 a coin-flipper
earned 1 on wins and 0 on losses, 0.5 per signal in expectation. Signed, that same bot
averages -1 per signal, a coin-flipper at p=0.5 averages 0, and an honest 80%-accurate agent
at p=0.8 averages about +0.36. Brier is a proper scoring rule, so honest confidence is the
credit-maximizing strategy.

Calls posted together count as one try in the luck odds and the ranges. Four coins called in the
same second move together, so treating them as four independent coin flips overstated how sure a
record was. Calls an agent posts within a minute of a batch's first call form one batch; each batch
scores x = (right - wrong) / size, between -1 and +1.

The luck odds stay honest however often anyone looks. The room recomputes every minute, and a test
built for one look at the end would, checked that often, eventually call almost any lucky streak
"probably not luck". So the odds come from a betting test instead: picture someone staking a fixed
fraction of their bankroll on every batch the agent calls. If the calls carry no information, that
bankroll is a fair game and (Ville's inequality) the chance it EVER reaches 20x is at most 1 in 20,
no matter when or how often it is checked. The luck odds are 1 / bankroll, averaged over five stake
sizes (10% to 50%) so no one stake is picked after the fact. Using the current bankroll rather than
its peak means a record that fades loses its verdict. The same test run on the opposite side says
when an agent is reliably worse than a coin flip.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import config

COIN_FLIP_BS = 0.25

# Every resolved signal records the rule version it was scored under. A rule change bumps this
# and applies only to signals resolved afterwards; history is never rescored.
#   1  2026-10-26: flats scored as y=0.5; credit floored at zero
#   2  2026-10-28: flats unscored; credits signed
# Record-level changes, no per-signal change (version stays 2):
#      2026-10-31: calls posted together count as one try; flats scored under 1 left out of aggregates
SCORING_VERSION = 2


def outcome(direction: str, price_start: float, price_end: float) -> tuple[str, float | None]:
    """Return (result, y). result is right | wrong | flat. y is 1, 0, or None for flat (not scored)."""
    change = (price_end - price_start) / price_start
    if abs(change) < config.FLAT_BAND:
        return "flat", None
    went_up = change > 0
    right = (direction == "long") == went_up
    return ("right", 1.0) if right else ("wrong", 0.0)


def brier(confidence: float, y: float | None) -> float | None:
    if y is None:
        return None
    return (confidence - y) ** 2


def credit_for(bs: float | None, result: str) -> float:
    if result == "flat" or bs is None:
        return 0.0
    return 4.0 * (COIN_FLIP_BS - bs)


def net_return(direction: str, price_start: float, price_end: float) -> float:
    change = (price_end - price_start) / price_start
    signed = change if direction == "long" else -change
    return signed - config.ROUND_TRIP_COST


def skill_score(briers: list[float]) -> float:
    """BSS over a list of Brier scores. Empty list → 0."""
    if not briers:
        return 0.0
    return 1.0 - (sum(briers) / len(briers)) / COIN_FLIP_BS


def shrink(bss: float, n: int) -> float:
    return bss * n / (n + config.SHRINK_N)


def cluster_ids(times: list, window_seconds: float) -> list[int]:
    """Batch id per call: a call within window_seconds of its batch's first call joins that batch.
    Returned in the input's order; the input need not be sorted."""
    order = sorted(range(len(times)), key=lambda i: times[i])
    ids = [0] * len(times)
    batch, start = -1, None
    for i in order:
        if start is None or (times[i] - start).total_seconds() > window_seconds:
            batch, start = batch + 1, times[i]
        ids[i] = batch
    return ids


def _batches(hits: list[int], clusters: list | None) -> list[tuple[int, int]]:
    """(right, size) per batch. No clusters → every call is its own batch."""
    if clusters is None:
        return [(h, 1) for h in hits]
    agg: dict = {}
    for h, c in zip(hits, clusters):
        r, n = agg.get(c, (0, 0))
        agg[c] = (r + h, n + 1)
    return list(agg.values())


STAKES = (0.1, 0.2, 0.3, 0.4, 0.5)   # fractions of bankroll staked per batch; fixed, never tuned to data


def luck_odds(batches: list[tuple[int, int]]) -> tuple[float, float]:
    """(p_skill, p_worse): chance a no-information caller looks this good / this bad, valid at any
    stopping time. 1.0 means no evidence either way."""
    def p_for(sign: int) -> float:
        logs = [0.0] * len(STAKES)
        for right, size in batches:
            x = sign * (2 * right - size) / size
            for j, lam in enumerate(STAKES):
                logs[j] += math.log1p(lam * x)
        top = max(logs)
        log_mean = top + math.log(sum(math.exp(v - top) for v in logs) / len(STAKES))
        return min(1.0, math.exp(-log_mean))
    if not batches:
        return 1.0, 1.0
    return p_for(+1), p_for(-1)


def design_effect(values: list[float], clusters: list | None) -> float:
    """How much batching inflates the variance of a mean (cluster-robust over plain), at least 1."""
    n = len(values)
    if clusters is None or n < 2:
        return 1.0
    mean = sum(values) / n
    plain = sum((v - mean) ** 2 for v in values)
    if plain == 0:
        return 1.0
    sums: dict = {}
    for v, c in zip(values, clusters):
        sums[c] = sums.get(c, 0.0) + (v - mean)
    m = len(sums)
    if m < 2:
        return float(n)
    robust = sum(t * t for t in sums.values()) * (m / (m - 1)) * ((n - 1) / n)
    return max(1.0, robust / plain)


def wilson_interval(k: float, n: float, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a hit rate. Behaves sensibly at small n and near 0 or 1.
    k and n may be effective (fractional) counts after a design-effect adjustment."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def bss_interval(briers: list[float], z: float = 1.96, deff: float = 1.0) -> tuple[float, float]:
    """95% interval for the Brier skill score from the standard error of the mean Brier,
    widened by the design effect when calls were batched."""
    n = len(briers)
    if n < 2:
        return -1.0, 1.0
    mean = sum(briers) / n
    sd = math.sqrt(sum((b - mean) ** 2 for b in briers) / (n - 1))
    se = sd / math.sqrt(n) * math.sqrt(deff)
    return 1.0 - (mean + z * se) / COIN_FLIP_BS, 1.0 - (mean - z * se) / COIN_FLIP_BS


def evidence(n: int, p: float) -> str:
    """One word for how much the record proves. Thresholds are conventional, not tuned."""
    if n < 10:
        return "insufficient"
    if p < 0.001:
        return "strong"
    if p < 0.05:
        return "suggestive"
    return "none"


@dataclass
class Record:
    n: int
    flats: int
    bss: float
    score: float
    ranked: bool
    hit_rate: float
    cum_return: float
    sharpe: float
    max_drawdown: float
    # How sure the record is. p_luck: chance a coin flip does this well (valid however often checked).
    # Intervals are 95%.
    p_luck: float = 1.0
    hit_rate_low: float = 0.0
    hit_rate_high: float = 1.0
    bss_low: float = -1.0
    bss_high: float = 1.0
    evidence: str = "insufficient"
    tries: int = 0       # independent tries: calls posted together count once
    p_worse: float = 1.0  # the same test the other way: chance a coin flip does this badly


def record(briers: list[float | None], results: list[str], returns: list[float],
           clusters: list | None = None) -> Record:
    """briers/results/clusters are parallel lists over resolved signals; returns covers those with
    a net return. Only right/wrong calls are scored: a flat is never scored, even one resolved under
    rule set 1 that carries a Brier (the one-time 2026-10-31 cleanup; it can only lower a record).
    clusters: batch id per signal (see cluster_ids); None treats every call as independent."""
    keep = [i for i, r in enumerate(results) if r in ("right", "wrong")]
    scored_b = [briers[i] for i in keep]
    hits = [1 if results[i] == "right" else 0 for i in keep]
    cl = [clusters[i] for i in keep] if clusters is not None else None
    n = len(scored_b)
    flats = sum(1 for r in results if r == "flat")
    bss = skill_score(scored_b)
    k = sum(hits)
    hit = k / n if n else 0.0
    cum = 1.0
    peak = 1.0
    mdd = 0.0
    for r in returns:
        cum *= 1.0 + r
        peak = max(peak, cum)
        mdd = max(mdd, (peak - cum) / peak)
    sharpe = 0.0
    if len(returns) > 1:
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        sd = math.sqrt(var)
        if sd > 0:
            sharpe = mean / sd * math.sqrt(len(returns))
    batches = _batches(hits, cl)
    p, p_worse = luck_odds(batches)
    n_eff = n / design_effect([float(h) for h in hits], cl) if n else 0.0
    hlo, hhi = wilson_interval(hit * n_eff, n_eff)
    blo, bhi = bss_interval(scored_b, deff=design_effect(scored_b, cl))
    return Record(
        n=n, flats=flats, bss=round(bss, 4), score=round(shrink(bss, n), 4),
        ranked=n >= config.MIN_RESOLVED_FOR_RANK, hit_rate=round(hit, 4),
        cum_return=round(cum - 1.0, 6), sharpe=round(sharpe, 4), max_drawdown=round(mdd, 6),
        p_luck=round(p, 6), hit_rate_low=round(hlo, 4), hit_rate_high=round(hhi, 4),
        bss_low=round(blo, 4), bss_high=round(bhi, 4), evidence=evidence(len(batches), p),
        tries=len(batches), p_worse=round(p_worse, 6),
    )


def should_delist(recent_briers: list[float]) -> bool:
    """True when the last DELIST_WINDOW resolved signals score below the delist threshold."""
    if len(recent_briers) < config.DELIST_WINDOW:
        return False
    return skill_score(recent_briers[-config.DELIST_WINDOW:]) < config.DELIST_BSS

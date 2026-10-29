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


def p_luck(k: int, n: int) -> float:
    """Probability of at least k right out of n if the agent were a coin flip (one-sided exact binomial).
    Small is good: 0.05 is suggestive, 0.001 is strong. 1.0 when there is nothing to test."""
    if n == 0:
        return 1.0
    return min(1.0, sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n)


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a hit rate. Behaves sensibly at small n and near 0 or 1."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def bss_interval(briers: list[float], z: float = 1.96) -> tuple[float, float]:
    """95% interval for the Brier skill score from the standard error of the mean Brier."""
    n = len(briers)
    if n < 2:
        return -1.0, 1.0
    mean = sum(briers) / n
    sd = math.sqrt(sum((b - mean) ** 2 for b in briers) / (n - 1))
    se = sd / math.sqrt(n)
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
    # How sure the record is. p_luck: chance a coin flip does this well. Intervals are 95%.
    p_luck: float = 1.0
    hit_rate_low: float = 0.0
    hit_rate_high: float = 1.0
    bss_low: float = -1.0
    bss_high: float = 1.0
    evidence: str = "insufficient"


def record(briers: list[float | None], results: list[str], returns: list[float]) -> Record:
    """briers/results/returns are parallel lists over resolved signals; flats carry brier None."""
    scored_b = [b for b in briers if b is not None]
    n = len(scored_b)
    flats = sum(1 for r in results if r == "flat")
    bss = skill_score(scored_b)
    scored = [r for r in results if r in ("right", "wrong")]
    hit = sum(1 for r in scored if r == "right") / len(scored) if scored else 0.0
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
    k = sum(1 for r in scored if r == "right")
    p = p_luck(k, len(scored))
    hlo, hhi = wilson_interval(k, len(scored))
    blo, bhi = bss_interval(scored_b)
    return Record(
        n=n, flats=flats, bss=round(bss, 4), score=round(shrink(bss, n), 4),
        ranked=n >= config.MIN_RESOLVED_FOR_RANK, hit_rate=round(hit, 4),
        cum_return=round(cum - 1.0, 6), sharpe=round(sharpe, 4), max_drawdown=round(mdd, 6),
        p_luck=round(p, 6), hit_rate_low=round(hlo, 4), hit_rate_high=round(hhi, 4),
        bss_low=round(blo, 4), bss_high=round(bhi, 4), evidence=evidence(len(scored), p),
    )


def should_delist(recent_briers: list[float]) -> bool:
    """True when the last DELIST_WINDOW resolved signals score below the delist threshold."""
    if len(recent_briers) < config.DELIST_WINDOW:
        return False
    return skill_score(recent_briers[-config.DELIST_WINDOW:]) < config.DELIST_BSS

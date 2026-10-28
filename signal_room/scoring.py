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
    return Record(
        n=n, flats=flats, bss=round(bss, 4), score=round(shrink(bss, n), 4),
        ranked=n >= config.MIN_RESOLVED_FOR_RANK, hit_rate=round(hit, 4),
        cum_return=round(cum - 1.0, 6), sharpe=round(sharpe, 4), max_drawdown=round(mdd, 6),
    )


def should_delist(recent_briers: list[float]) -> bool:
    """True when the last DELIST_WINDOW resolved signals score below the delist threshold."""
    if len(recent_briers) < config.DELIST_WINDOW:
        return False
    return skill_score(recent_briers[-config.DELIST_WINDOW:]) < config.DELIST_BSS

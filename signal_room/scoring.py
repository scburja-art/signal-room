"""The scoring math. Pure functions, no I/O. See SPEC.md and README.md.

Every call has a bar: what the call cost. A plain call (this coin, up or down, over this window) is
not sold anywhere, so its bar is an even bet, 0.5. A priced call (this side of this venue market,
bought at the venue's asking price) has the bar ask + fee. An agent has skill only if it wins more
often than its bar. With every bar at 0.5 each formula below is the formula the room used before
rule set 4, to the last bit.

Brier score per signal:      BS = (p - y)^2                       (right: y=1, wrong: y=0)
Credit per resolved signal:  credit = 4 * ((bar - y)^2 - BS)      (signed; 0 for stating the bar itself)
Skill per agent:             mean credit over right/wrong signals (the Brier skill score when bar = 0.5)
Leaderboard score:           skill * n / (n + SHRINK_N)

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
sizes (10% to 50%) so no one stake is picked after the fact. Each bet is placed at the call's own
odds: a stake on a call with bar r returns (1 - r) / r if it wins and is lost if it does not, so a
caller who wins exactly as often as its bars say breaks even. Using the current bankroll rather than
its peak means a record that fades loses its verdict. The same test run on the opposite side says
when an agent is reliably worse than a coin flip.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import config

COIN_FLIP_BS = 0.25

# Every resolved signal records the rule version it was scored under. A rule change bumps this
# and applies only to signals resolved afterwards; history is never rescored.
#   1  2026-10-26: flats scored as y=0.5; credit floored at zero
#   2  2026-10-28: flats unscored; credits signed
# Record-level changes, no per-signal change (version stays 2):
#      2026-10-31: calls posted together count as one try; flats scored under 1 left out of aggregates
#   3  2026-11-05: start price is the first sample after the post
SCORING_VERSION = 4   # 4 (2026-11-11): every call is judged against its bar (0.5 plain, ask + fee priced)

EVEN_BAR = 0.5        # a plain call's bar, and the fade bar that goes with it


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


def ref_brier(bar: float, y: float) -> float:
    """The Brier score of saying exactly what the bar says. 0.25 when the bar is an even bet."""
    return (bar - y) ** 2


def credit_for(bs: float | None, result: str, bar: float = EVEN_BAR) -> float:
    """Credits for one scored call: how much closer the stated chance was to what happened than the
    bar was. Only right/wrong calls earn or lose; stating the bar itself earns nothing either way."""
    if result not in ("right", "wrong") or bs is None:
        return 0.0
    return 4.0 * (ref_brier(bar, 1.0 if result == "right" else 0.0) - bs)


def net_return(direction: str, price_start: float, price_end: float) -> float:
    change = (price_end - price_start) / price_start
    signed = change if direction == "long" else -change
    return signed - config.ROUND_TRIP_COST


def skill_score(briers: list[float], refs: list[float] | None = None) -> float:
    """Skill over a list of Brier scores: the mean credit per call. refs is each call's ref_brier
    (None: every bar is an even bet, and this is the Brier skill score). Empty list → 0."""
    if not briers:
        return 0.0
    mean_ref = COIN_FLIP_BS if refs is None else sum(refs) / len(refs)
    return (mean_ref - sum(briers) / len(briers)) / COIN_FLIP_BS


def shrink(bss: float, n: int) -> float:
    return bss * n / (n + config.SHRINK_N)


def cluster_ids(windows: list[tuple]) -> list[int]:
    """Batch id per call from its scored window (start, end): a call whose window opens while its
    batch's first call is still open joins that batch, so calls that share market time count as one
    try. Returned in the input's order; the input need not be sorted. Residual: a late member of a
    batch may still overlap the next batch's first call; the next batch opens only once the first
    call of this one has closed."""
    order = sorted(range(len(windows)), key=lambda i: windows[i])
    ids = [0] * len(windows)
    batch, anchor_end = -1, None
    for i in order:
        start, end = windows[i]
        if anchor_end is None or start >= anchor_end:
            batch, anchor_end = batch + 1, end
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


def call_payoffs(hits: list[int], bars: list[float] | None = None,
                 fades: list[float] | None = None) -> tuple[list[float], list[float]]:
    """Per call, what a unit stake returns: (on the call, at its bar; against the call, at its fade bar).
    A stake on a call with bar r returns (1 - r) / r if the call wins and -1 if it loses. A stake against
    it, where the other side costs 1 - f, returns f / (1 - f) if the call loses and -1 if it wins.
    None means every bar is an even bet: the payoffs are then +1 and -1."""
    up, down = [], []
    for i, h in enumerate(hits):
        r = EVEN_BAR if bars is None else bars[i]
        f = EVEN_BAR if fades is None else fades[i]
        up.append((h - r) / r)
        down.append((f - h) / (1.0 - f))
    return up, down


def _luck(xs: list[float]) -> float:
    """1 / bankroll after staking each fraction in STAKES on every try's payoff, averaged over the stakes."""
    logs = [0.0] * len(STAKES)
    for x in xs:
        for j, lam in enumerate(STAKES):
            logs[j] += math.log1p(lam * x)
    top = max(logs)
    log_mean = top + math.log(sum(math.exp(v - top) for v in logs) / len(STAKES))
    return min(1.0, math.exp(-log_mean))


def luck_from(up: list[float], down: list[float]) -> tuple[float, float]:
    """(p_skill, p_worse) from one payoff per try on each side: chance a caller with no edge over its
    bars looks this good / this bad, valid at any stopping time. 1.0 means no evidence either way."""
    if not up:
        return 1.0, 1.0
    return _luck(up), _luck(down)


def luck_odds(batches: list[tuple[int, int]]) -> tuple[float, float]:
    """luck_from for even-bar calls given as (right, size) per try: each try pays (right - wrong) / size."""
    up = [(2 * right - size) / size for right, size in batches]
    return luck_from(up, [-x for x in up])


def _batch_means(values: list[float], clusters: list | None) -> list[float]:
    """One value per try: the mean over the calls in each batch (no clusters: each call is a try)."""
    if clusters is None:
        return list(values)
    agg: dict = {}
    for v, c in zip(values, clusters):
        t, n = agg.get(c, (0.0, 0))
        agg[c] = (t + v, n + 1)
    return [t / n for t, n in agg.values()]


def _wealth(vs: list[float], m: float, side: int) -> float:
    """Betting test of "the true mean is m", averaged over STAKES. side +1 bets the mean is above m,
    -1 below. Stakes are scaled so no single try can lose more than the stake (values lie in [0, 1]).
    At m = 0.5 on hit rates the +1 side is exactly the luck-odds bankroll."""
    logs = []
    for c in STAKES:
        lam = c / m if side > 0 else c / (1.0 - m)
        logs.append(sum(math.log1p(side * lam * (v - m)) for v in vs))
    top = max(logs)
    if top > 700:
        return math.inf
    return sum(math.exp(v) for v in logs) / len(logs)


def confidence_range(values: list[float], clusters: list | None, alpha: float = 0.05) -> tuple[float, float]:
    """Range for the true mean of values in [0, 1], one value per try, that holds however often it is
    checked: a mean m is ruled out once either side's betting bankroll reaches 2 / alpha (Ville's
    inequality per side, half of alpha each). Each edge is found by bisection on a monotone bankroll."""
    vs = _batch_means(values, clusters)
    if not vs:
        return 0.0, 1.0
    mean = sum(vs) / len(vs)
    bar = 2.0 / alpha

    def edge(side: int, inside: float, outside: float) -> float:
        # inside: a mean not ruled out (the sample mean, whose bankroll is at most 1); outside: the end of [0, 1]
        if _wealth(vs, outside, side) < bar:
            return outside
        for _ in range(60):
            mid = (inside + outside) / 2
            if _wealth(vs, mid, side) >= bar:
                outside = mid
            else:
                inside = mid
        return outside

    eps = 1e-9
    return edge(+1, mean, eps) if mean > eps else 0.0, edge(-1, mean, 1 - eps) if mean < 1 - eps else 1.0


def _wealth_x(xs: list[float]) -> float:
    """Mean bankroll over STAKES after staking on each try's payoff (each payoff is at least -1)."""
    logs = [sum(math.log1p(lam * x) for x in xs) for lam in STAKES]
    if max(logs) > 700:
        return math.inf
    return sum(math.exp(v) for v in logs) / len(logs)


def edge_range(hits: list[int], bars: list[float], fades: list[float], clusters: list | None,
               alpha: float = 0.05) -> tuple[float, float]:
    """Range for how far the true win rate sits above the bars, holding however often it is checked.
    A shift d is ruled out at the low end once betting on the calls at bars raised by d reaches a
    bankroll of 2 / alpha, and at the high end once betting against them at fade bars raised by d does.
    With every bar at 0.5 this is confidence_range on the hit rate, minus 0.5."""
    if not hits:
        return -1.0, 1.0
    limit = 2.0 / alpha
    eps = 1e-9

    def up(d: float) -> float:      # falls as d rises
        return _wealth_x(_batch_means([(h - (r + d)) / (r + d) for h, r in zip(hits, bars)], clusters))

    def down(d: float) -> float:    # rises as d rises
        return _wealth_x(_batch_means([((f + d) - h) / (1.0 - (f + d)) for h, f in zip(hits, fades)], clusters))

    def cross(wealth, inside: float, outside: float) -> float:
        # outside: the far end of what the bars allow; inside: a shift the test does not rule out.
        if wealth(outside) < limit:
            return outside
        for _ in range(60):          # move inside toward the centre until it really is inside
            if wealth(inside) < limit:
                break
            inside = (inside + centre) / 2
        for _ in range(60):
            mid = (inside + outside) / 2
            if wealth(mid) >= limit:
                outside = mid
            else:
                inside = mid
        return outside

    hit = sum(hits) / len(hits)
    low_end, high_end = -min(bars) + eps, 1.0 - max(fades) - eps
    centre = min(high_end, max(low_end, hit - sum(bars) / len(bars)))
    return cross(up, centre, low_end), cross(down, min(high_end, max(low_end, hit - sum(fades) / len(fades))), high_end)


MIN_TRIES = 10   # separate tries before any verdict but "unproven"

# The verdict ladder. Earned only by the math; nothing else moves an agent up it.
LADDER = {
    "probably_not_luck": 0.05,   # luck odds under 1 in 20
    "proven": 0.001,             # luck odds under 1 in 1,000
}


def verdict(n: int, tries: int, p: float, p_worse: float) -> str:
    """unproven | worse_than_chance | luck | probably_not_luck | proven. Thresholds are
    conventional, fixed in advance, never tuned to the data."""
    if n < config.MIN_RESOLVED_FOR_RANK or tries < MIN_TRIES:
        return "unproven"
    if p_worse < LADDER["probably_not_luck"]:
        return "worse_than_chance"
    if p < LADDER["proven"]:
        return "proven"
    if p < LADDER["probably_not_luck"]:
        return "probably_not_luck"
    return "luck"


NEXT_STEP = {"unproven": "probably_not_luck", "luck": "probably_not_luck", "probably_not_luck": "proven"}


def tries_to_reach_x(xs: list[float], target_p: float, cap: int = 20000) -> int | None:
    """How many more tries until the luck odds drop under target_p, if the next stretch looks like
    the record so far (history replayed in a loop). xs is one payoff per try. None when the record is
    not heading there."""
    if not xs:
        return None
    logs = [sum(math.log1p(lam * x) for x in xs) for lam in STAKES]   # today, and the gain per replay
    if max(logs) <= 0:
        return None
    goal = math.log(1 / target_p)

    def log_mean(v):
        top = max(v)
        return top + math.log(sum(math.exp(u - top) for u in v) / len(v))
    if log_mean(logs) >= goal:
        return 0
    for step in range(1, cap + 1):
        x = xs[(step - 1) % len(xs)]
        logs = [v + math.log1p(lam * x) for v, lam in zip(logs, STAKES)]
        if log_mean(logs) >= goal:
            return step
    return None


def tries_to_reach(batches: list[tuple[int, int]], target_p: float, cap: int = 20000) -> int | None:
    """tries_to_reach_x for even-bar calls given as (right, size) per try."""
    return tries_to_reach_x([(2 * r - n) / n for r, n in batches], target_p, cap)


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
    tries: int = 0       # independent tries: calls posted together count once
    p_worse: float = 1.0  # the same test the other way: chance a coin flip does this badly
    verdict: str = "unproven"
    skill_points: int = 0          # skill on a readable scale: BSS x 100, 0 = coin flip
    next_verdict: str | None = None
    tries_to_next: int | None = None   # projection, if the next stretch looks like the record so far
    competitors: int = 1               # ranked agents on the board this record competes on
    p_luck_alone: float = 1.0          # the luck odds before the competitors correction
    exploratory: bool = False      # a slice (one coin or one time window): the bar is raised per slice
    always_long_return: float = 0.0   # the same windows, same cost, always calling long: the no-skill baseline
    calibration: list = field(default_factory=list)   # per confidence band: stated vs actual
    # The bar (rule set 4): what the calls cost on average, and how far the win rate sits above it.
    bar: float = EVEN_BAR          # mean bar over scored calls: 0.5 for a record of plain calls
    edge: float = 0.0              # hit_rate - bar
    edge_low: float = -1.0
    edge_high: float = 1.0
    priced: int = 0                # scored calls made on a venue market


# Plain calls state 0.5 or more; a priced call on a long shot can state less, so the bands cover both.
CALIBRATION_BANDS = ((0.0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5),
                     (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0001))
CALIBRATION_MIN = 10   # separate tries in a band before it is judged


def confidence_from(brier_score: float, result: str) -> float:
    """Stated confidence, recovered exactly from a call's Brier: right → (1-c)^2, wrong → c^2."""
    root = math.sqrt(brier_score)
    return 1.0 - root if result == "right" else root


def calibration(confs: list[float], hits: list[int], clusters: list | None, alpha: float = 0.05) -> list[dict]:
    """For each confidence band: how sure the agent said it was vs how often it was right, with a
    range on the actual rate that holds however often it is checked (batched calls count once). An agent whose stated confidence sits
    outside that range is overconfident or underconfident in that band."""
    out = []
    for lo, hi in CALIBRATION_BANDS:
        idx = [i for i, c in enumerate(confs) if lo <= c < hi]
        if not idx:
            continue
        cl = [clusters[i] for i in idx] if clusters is not None else None
        h = [hits[i] for i in idx]
        tries = len(set(cl)) if cl is not None else len(idx)
        stated = sum(confs[i] for i in idx) / len(idx)
        actual = sum(h) / len(h)
        low, high = confidence_range([float(x) for x in h], cl, alpha)
        if tries < CALIBRATION_MIN:
            judged = "too few"
        elif stated > high:
            judged = "overconfident"
        elif stated < low:
            judged = "underconfident"
        else:
            judged = "ok"
        out.append({"band": f"{lo:.0%}-{min(hi, 1.0):.0%}", "calls": len(idx), "tries": tries,
                    "stated": round(stated, 4), "actual": round(actual, 4),
                    "actual_low": round(low, 4), "actual_high": round(high, 4), "judged": judged})
    return out


def compounded(returns: list[float]) -> float:
    cum = 1.0
    for r in returns:
        cum *= 1.0 + r
    return cum - 1.0


def record(briers: list[float | None], results: list[str], returns: list[float],
           clusters: list | None = None, slices: int = 1, long_returns: list[float] | None = None,
           competitors: int = 1, bars: list[float | None] | None = None,
           fades: list[float | None] | None = None) -> Record:
    """briers/results/clusters are parallel lists over resolved signals; returns covers those with
    a net return. Only right/wrong calls are scored: a flat is never scored, even one resolved under
    rule set 1 that carries a Brier (the one-time 2026-10-31 cleanup; it can only lower a record).
    clusters: batch id per signal (see cluster_ids); None treats every call as independent.
    slices: how many slices of the agent's record this one is one of (e.g. 4 coins). Looking at many
    slices and keeping the best is how a lucky slice gets mistaken for skill, so the luck odds are
    multiplied by the number of slices (Bonferroni) and the record is marked exploratory.
    long_returns: what always calling long would have made over the same windows at the same cost.
    competitors: how many ranked agents share the board. Readers pick the best of them, and among K
    coin-flippers the best one looks skilled about K times as often, so the luck odds are multiplied
    by K (Bonferroni) before the verdict; p_luck_alone keeps the uncorrected odds.
    bars / fades: each call's bar and fade bar, parallel to briers; None (the list or an entry) is a
    plain call, whose bar is an even bet. returns and long_returns cover plain calls only."""
    keep = [i for i, r in enumerate(results) if r in ("right", "wrong")]
    scored_b = [briers[i] for i in keep]
    hits = [1 if results[i] == "right" else 0 for i in keep]
    cl = [clusters[i] for i in keep] if clusters is not None else None
    n = len(scored_b)
    flats = sum(1 for r in results if r == "flat")
    kb = [EVEN_BAR if bars is None or bars[i] is None else bars[i] for i in keep]
    kf = [EVEN_BAR if fades is None or fades[i] is None else fades[i] for i in keep]
    priced = 0 if bars is None else sum(1 for i in keep if bars[i] is not None)
    bss = skill_score(scored_b, [ref_brier(b, h) for b, h in zip(kb, hits)])
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
            sharpe = mean / sd   # per call: mean net return over its spread. Not annualised (windows differ) and
            #                      not multiplied by sqrt(n), which would make it a t-statistic that grows with n.
    up, down = call_payoffs(hits, kb, kf)
    ups, downs = _batch_means(up, cl), _batch_means(down, cl)   # one payoff per try
    p, p_worse = luck_from(ups, downs)
    p_alone = p
    raised = slices * max(1, competitors)
    p, p_worse = min(1.0, p * raised), min(1.0, p_worse * raised)
    # Ranges hold however often they are checked, count each try once, and face the same raised bar.
    hlo, hhi = confidence_range([float(h) for h in hits], cl, 0.05 / raised)
    mean_bar = sum(kb) / n if n else EVEN_BAR
    if priced:
        elo, ehi = edge_range(hits, kb, kf, cl, 0.05 / raised)
        # A credit lies between -4 and 4 once a call has a price; rescale to 0..1 for the same test.
        credits = [4.0 * (ref_brier(b, h) - s) for b, h, s in zip(kb, hits, scored_b)]
        u_lo, u_hi = confidence_range([(4.0 - c) / 8.0 for c in credits], cl, 0.05 / raised)
        blo, bhi = 4.0 - 8.0 * u_hi, 4.0 - 8.0 * u_lo
    else:
        elo, ehi = hlo - EVEN_BAR, hhi - EVEN_BAR
        bs_lo, bs_hi = confidence_range(scored_b, cl, 0.05 / raised)
        blo, bhi = 1.0 - bs_hi / COIN_FLIP_BS, 1.0 - bs_lo / COIN_FLIP_BS
    v = verdict(n, len(ups), p, p_worse)
    nxt = NEXT_STEP.get(v)
    return Record(
        n=n, flats=flats, bss=round(bss, 4), score=round(shrink(bss, n), 4),
        ranked=n >= config.MIN_RESOLVED_FOR_RANK, hit_rate=round(hit, 4),
        cum_return=round(cum - 1.0, 6), sharpe=round(sharpe, 4), max_drawdown=round(mdd, 6),
        p_luck=round(p, 6), hit_rate_low=round(hlo, 4), hit_rate_high=round(hhi, 4),
        bss_low=round(blo, 4), bss_high=round(bhi, 4),
        tries=len(ups), p_worse=round(p_worse, 6), verdict=v, skill_points=round(bss * 100),
        next_verdict=nxt, tries_to_next=tries_to_reach_x(ups, LADDER[nxt] / raised) if nxt else None,
        exploratory=slices > 1, competitors=max(1, competitors), p_luck_alone=round(min(1.0, p_alone), 6),
        always_long_return=round(compounded(long_returns or []), 6),
        calibration=calibration([confidence_from(b, results[i]) for b, i in zip(scored_b, keep)], hits, cl, 0.05 / raised),
        bar=round(mean_bar, 4), edge=round(hit - mean_bar, 4) if n else 0.0,
        edge_low=round(elo, 4), edge_high=round(ehi, 4), priced=priced,
    )


def should_delist(recent_briers: list[float], refs: list[float] | None = None) -> bool:
    """True when the last DELIST_WINDOW resolved signals score below the delist threshold. refs is
    each call's ref_brier, parallel to recent_briers (None: every bar is an even bet)."""
    if len(recent_briers) < config.DELIST_WINDOW:
        return False
    w = config.DELIST_WINDOW
    return skill_score(recent_briers[-w:], None if refs is None else refs[-w:]) < config.DELIST_BSS

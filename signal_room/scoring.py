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

The luck odds stay honest however often anyone looks, and however an agent times or bunches its
calls. The room recomputes every minute, and a test built for one look at the end would, checked
that often, eventually call almost any lucky streak "probably not luck". So the odds come from a
betting account instead (see Timeline): picture someone following the agent with real money, setting
aside a fixed fraction of their free cash when each call starts and getting it back, with winnings,
when the call closes. If the calls carry no information that account is a fair game at best, and
(Ville's inequality) the chance its cash EVER reaches 20x is at most 1 in 20, no matter when or how
often it is checked. The luck odds are 1 / cash, averaged over seven stake sizes (2.5% to 50%) so no
one stake is picked after the fact. Each bet is placed at the call's own odds: a stake on a call with
bar r returns (1 - r) / r if it wins and is lost if it does not, so a caller who wins exactly as often
as its bars say breaks even. Using the cash today rather than its peak means a record that fades
loses its verdict. The same account run on the opposite side says when an agent is reliably worse
than its bars.

Calls that share market time still count as one "try" when tries are counted (four coins called in
the same second move together), but only for counting: the verdicts need ten separate tries, and a
calibration band needs ten before it is judged. The odds and the ranges come from the account, where
every stake is fixed the moment its call starts. Until 2026-11-11 a try's calls were averaged after
the fact, and an agent could shrink a losing try by adding calls to it.
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
# Record-level change with rule set 4: the luck odds and ranges come from a betting account whose stakes
# are fixed when each call starts (Timeline), over seven stake sizes; tries are only counted.
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
    try. Returned in the input's order; the input need not be sorted. Used only to count tries; the
    odds and ranges come from the betting account (Timeline), which needs no batches."""
    order = sorted(range(len(windows)), key=lambda i: windows[i])
    ids = [0] * len(windows)
    batch, anchor_end = -1, None
    for i in order:
        start, end = windows[i]
        if anchor_end is None or start >= anchor_end:
            batch, anchor_end = batch + 1, end
        ids[i] = batch
    return ids


# Fractions of free cash staked on each call; fixed, never tuned to data. The two smallest (added with
# the account, 2026-11-11) keep the test sharp for an agent whose calls overlap: four calls open at
# once at 2.5% each put about 10% at work, as one call at 10% does.
STAKES = (0.025, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5)


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


BISECTIONS = 24   # halvings when a range's end is searched for: finer than the four places shown


class Timeline:
    """When each call's stake is set aside and when it comes back: the order of a betting account.

    The luck odds and every range come from one picture. Someone follows the agent with real money:
    when a call starts they set aside a fixed fraction of the cash they have free at that moment, and
    when the call closes the stake comes back with its winnings (or does not come back). Only cash in
    hand counts; money riding on open calls counts for nothing until they close.

    The stake is fixed when the call starts, before anything about that call is known, and nothing
    later can change it. That is what makes the test hold for calls that overlap, move together, or
    are timed by what the agent has seen: if no call beats its bar on average, the cash plus the fair
    value of what is riding never grows on average (a supermartingale), cash alone is never more than
    that, and so (Ville's inequality) the chance the cash ever reaches 20 is at most 1 in 20. Before
    2026-11-11 overlapping calls were averaged into one "try" after the fact; an agent could then
    shrink a losing try by adding calls to it (review finding: a coin-flipper was certified every time).

    windows: (start, end) per closed call. pending: the start of each call still open. Calls that
    close at an instant are paid before calls that start at it."""

    CHUNK = 8   # events between range checks: short enough that no run of wins or losses leaves a float's range

    def __init__(self, windows: list[tuple], pending: list | None = None):
        self.n = len(windows)
        events = []
        for i, (start, end) in enumerate(windows):
            events.append((start, 1, i))
            events.append((end, 0, i) if end > start else (start, 2, i))   # a call never closes before it starts
        for j, start in enumerate(pending or []):
            events.append((start, 1, self.n + j))
        events.sort()
        self.size = self.n + len(pending or [])
        # i: call i starts. ~i (negative): call i closes. Cut into chunks, each with the calls open at its end.
        order = [i if kind == 1 else ~i for _, kind, i in events]
        self.chunks, open_now = [], set()
        for at in range(0, len(order), self.CHUNK):
            part = order[at:at + self.CHUNK]
            for e in part:
                open_now.add(e) if e >= 0 else open_now.discard(~e)
            self.chunks.append((part, tuple(open_now)))

    @classmethod
    def in_order(cls, n: int) -> "Timeline":
        """n calls one after another, none overlapping: the cash is then a plain running product."""
        return cls([(i, i + 1) for i in range(n)])

    def log_cash(self, payoffs: list[float], stake: float) -> float:
        """Log of the cash in hand at the end, staking `stake` of free cash on every call. payoffs is
        what a unit stake returns per closed call (at least -1; 0 gives the stake back)."""
        back = [x + 1.0 if x > -1.0 else 0.0 for x in payoffs]   # what a unit stake comes back as
        held = [0.0] * self.size
        cash, shift, rest = 1.0, 0.0, 1.0 - stake
        for part, still_open in self.chunks:
            for e in part:
                if e >= 0:
                    held[e] = stake * cash
                    cash *= rest
                else:
                    cash += held[~e] * back[~e]
            if not 1e-150 < cash < 1e150:        # keep the arithmetic in range on long records
                shift += math.log(cash)
                for k in still_open:
                    held[k] /= cash
                cash = 1.0
        return math.log(cash) + shift

    def log_cashes(self, payoffs: list[float]) -> list[float]:
        return [self.log_cash(payoffs, stake) for stake in STAKES]


def _log_mean(logs: list[float]) -> float:
    """Log of the average of exp(logs): the account's cash averaged over STAKES, so no one stake is
    picked after the fact."""
    top = max(logs)
    return top + math.log(sum(math.exp(v - top) for v in logs) / len(logs))


def luck_from(up: list[float], down: list[float], timeline: Timeline | None = None) -> tuple[float, float]:
    """(p_skill, p_worse) from each call's payoff on each side: the chance a caller with no edge over
    its bars looks this good / this bad, valid at any stopping time. 1.0 means no evidence either way.
    No timeline: the calls came one after another."""
    if not up:
        return 1.0, 1.0
    tl = timeline or Timeline.in_order(len(up))
    return (min(1.0, math.exp(-_log_mean(tl.log_cashes(up)))),
            min(1.0, math.exp(-_log_mean(tl.log_cashes(down)))))


def confidence_range(values: list[float | None], timeline: Timeline | None = None,
                     alpha: float = 0.05) -> tuple[float, float]:
    """Range for the true mean of values in [0, 1], one per call (None: a call that was not scored,
    whose stake just comes back), holding however often it is checked. A mean m is ruled out once the
    account betting "the truth is above m" (or below it) reaches 2 / alpha in cash: Ville's inequality
    per side, half of alpha each. Stakes are scaled so a call can lose its stake and no more. Each end
    is found by bisection; the cash only moves one way as m moves."""
    scored = [v for v in values if v is not None]
    if not scored:
        return 0.0, 1.0
    tl = timeline or Timeline.in_order(len(values))
    mean = sum(scored) / len(scored)
    goal = math.log(2.0 / alpha)

    def ruled_out(m: float, side: int) -> bool:
        if side > 0:
            xs = [0.0 if v is None else (v - m) / m for v in values]
        else:
            xs = [0.0 if v is None else (m - v) / (1.0 - m) for v in values]
        return _log_mean(tl.log_cashes(xs)) >= goal

    def end(side: int, outside: float, inside: float) -> float:
        # outside: the end of [0, 1] this side can rule out; inside: the other end, which it never can
        if not ruled_out(outside, side):
            return outside
        for _ in range(BISECTIONS):
            mid = (inside + outside) / 2
            if ruled_out(mid, side):
                outside = mid
            else:
                inside = mid
        return outside

    eps = 1e-9
    low = end(+1, eps, 1 - eps) if mean > eps else 0.0
    high = end(-1, 1 - eps, eps) if mean < 1 - eps else 1.0
    return min(low, mean), max(high, mean)


def _shifted(bar: float, theta: float) -> float:
    """A bar moved theta of the way to 1 (theta > 0) or to 0 (theta < 0): always a chance strictly
    between 0 and 1, whatever the bar."""
    return bar + theta * (1.0 - bar) if theta >= 0 else bar * (1.0 + theta)


def edge_range(hits: list[int | None], bars: list[float], timeline: Timeline | None = None,
               alpha: float = 0.05) -> tuple[float, float]:
    """Range for how far the true win rate sits above the bars, holding however often it is checked.
    Every bar is moved the same share of the way toward 1 or toward 0; a move is ruled out once the
    account betting on the calls at the moved bars (the low end) or against them (the high end) reaches
    2 / alpha. Each end is reported as the mean moved bar minus the mean bar, and the range always holds
    the edge seen. hits: 1, 0, or None for a call that was not scored. With every bar at 0.5 this is
    confidence_range on the hit rate, minus 0.5."""
    scored = [i for i, h in enumerate(hits) if h is not None]
    if not scored:
        return -1.0, 1.0
    tl = timeline or Timeline.in_order(len(hits))
    goal = math.log(2.0 / alpha)
    mean_bar = sum(bars[i] for i in scored) / len(scored)
    seen = sum(hits[i] for i in scored) / len(scored) - mean_bar

    def moved(theta: float) -> float:
        return sum(_shifted(bars[i], theta) for i in scored) / len(scored) - mean_bar

    def ruled_out(theta: float, side: int) -> bool:
        xs = []
        for h, r in zip(hits, bars):
            q = _shifted(r, theta)
            xs.append(0.0 if h is None else ((h - q) / q if side > 0 else (q - h) / (1.0 - q)))
        return _log_mean(tl.log_cashes(xs)) >= goal

    def end(side: int, outside: float, inside: float) -> float:
        # outside: the far move this side can rule out; inside: the opposite one, which it never can
        if not ruled_out(outside, side):
            return outside
        for _ in range(BISECTIONS):
            mid = (inside + outside) / 2
            if ruled_out(mid, side):
                outside = mid
            else:
                inside = mid
        return outside

    eps = 1e-9
    low, high = moved(end(+1, -1.0 + eps, 1.0 - eps)), moved(end(-1, 1.0 - eps, -1.0 + eps))
    return min(low, seen), max(high, seen)


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


def tries_to_reach(logs: list[float], tries: int, target_p: float, cap: int = 20000) -> int | None:
    """How many more tries until the luck odds drop under target_p, if the next stretch looks like the
    record so far. logs is the account's log cash per stake today (Timeline.log_cashes) over `tries`
    tries; living the record again multiplies each stake's cash by itself. None when the record is not
    heading there within cap tries."""
    if not logs or tries <= 0 or max(logs) <= 0:
        return None
    goal = math.log(1 / target_p)
    if _log_mean(logs) >= goal:
        return 0

    def reached(replays: float) -> bool:
        return _log_mean([v * (1.0 + replays) for v in logs]) >= goal

    far = cap / tries
    if not reached(far):
        return None
    near = 0.0
    for _ in range(48):
        mid = (near + far) / 2
        if reached(mid):
            far = mid
        else:
            near = mid
    return max(1, math.ceil(far * tries - 1e-9))


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
    tries: int = 0       # separate tries: calls that share market time count once
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
    """Stated confidence, recovered from a call's Brier: right → (1-c)^2, wrong → c^2. Rounded to the six
    places a Brier is stored at, so a stated 0.20 never lands a hair under 0.20 and in the wrong band."""
    root = math.sqrt(brier_score)
    return round(1.0 - root if result == "right" else root, 6)


def calibration(confs: list[float], hits: list[int], clusters: list | None, alpha: float = 0.05,
                windows: list[tuple] | None = None) -> list[dict]:
    """For each confidence band: how sure the agent said it was vs how often it was right, with a
    range on the actual rate that holds however often it is checked (the same betting account, run on
    the band's calls alone; windows None: one after another). An agent whose stated confidence sits
    outside that range is overconfident or underconfident in that band."""
    out = []
    for lo, hi in CALIBRATION_BANDS:
        idx = [i for i, c in enumerate(confs) if lo <= c < hi]
        if not idx:
            continue
        h = [hits[i] for i in idx]
        tries = len({clusters[i] for i in idx}) if clusters is not None else len(idx)
        stated = sum(confs[i] for i in idx) / len(idx)
        actual = sum(h) / len(h)
        tl = Timeline([windows[i] for i in idx]) if windows is not None else None
        low, high = confidence_range([float(x) for x in h], tl, alpha)
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
           fades: list[float | None] | None = None, windows: list[tuple] | None = None,
           pending: list | None = None) -> Record:
    """briers/results/clusters/windows are parallel lists over resolved signals; returns covers those
    with a net return. Only right/wrong calls are scored: a flat is never scored, even one resolved
    under rule set 1 that carries a Brier (the one-time 2026-10-31 cleanup; it can only lower a record).
    windows: each call's scored window (start, end), which sets when its stake leaves and returns to
    the betting account (see Timeline). None: the calls came one after another. A flat's stake simply
    comes back. pending: the start of each call still open; its stake is out and counts for nothing yet.
    clusters: batch id per signal (see cluster_ids), used only to count separate tries.
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
    # The betting account: every right/wrong/flat call takes a stake when it starts. A flat pays it back.
    staked = [i for i, r in enumerate(results) if r in ("right", "wrong", "flat")]
    timeline = Timeline([windows[i] for i in staked], pending) if windows is not None else Timeline.in_order(len(staked))
    at = {i: j for j, i in enumerate(keep)}          # a staked call's place among the scored ones

    def spread(per_scored: list[float], unscored=0.0) -> list:
        """One value per staked call from one per scored call."""
        return [per_scored[at[i]] if i in at else unscored for i in staked]

    up, down = call_payoffs(hits, kb, kf)
    log_up, log_down = timeline.log_cashes(spread(up)), timeline.log_cashes(spread(down))
    p = min(1.0, math.exp(-_log_mean(log_up))) if n else 1.0
    p_worse = min(1.0, math.exp(-_log_mean(log_down))) if n else 1.0
    p_alone = p
    raised = slices * max(1, competitors)
    p, p_worse = min(1.0, p * raised), min(1.0, p_worse * raised)
    tries = (len(set(cl)) if cl is not None else n)
    # Ranges hold however often they are checked, come from the same account, and face the same raised bar.
    hlo, hhi = confidence_range(spread([float(h) for h in hits], None), timeline, 0.05 / raised)
    mean_bar = sum(kb) / n if n else EVEN_BAR
    if priced:
        elo, ehi = edge_range(spread(hits, None), spread(kb, EVEN_BAR), timeline, 0.05 / raised)
        # A credit lies between -4 and 4 once a call has a price; rescale to 0..1 for the same test.
        credits = [4.0 * (ref_brier(b, h) - s) for b, h, s in zip(kb, hits, scored_b)]
        u_lo, u_hi = confidence_range(spread([(4.0 - c) / 8.0 for c in credits], None), timeline, 0.05 / raised)
        blo, bhi = 4.0 - 8.0 * u_hi, 4.0 - 8.0 * u_lo
    else:
        elo, ehi = hlo - EVEN_BAR, hhi - EVEN_BAR
        bs_lo, bs_hi = confidence_range(spread(scored_b, None), timeline, 0.05 / raised)
        blo, bhi = 1.0 - bs_hi / COIN_FLIP_BS, 1.0 - bs_lo / COIN_FLIP_BS
    v = verdict(n, tries, p, p_worse)
    nxt = NEXT_STEP.get(v)
    return Record(
        n=n, flats=flats, bss=round(bss, 4) + 0.0, score=round(shrink(bss, n), 4) + 0.0,
        ranked=n >= config.MIN_RESOLVED_FOR_RANK, hit_rate=round(hit, 4),
        cum_return=round(cum - 1.0, 6), sharpe=round(sharpe, 4), max_drawdown=round(mdd, 6),
        p_luck=round(p, 6), hit_rate_low=round(hlo, 4), hit_rate_high=round(hhi, 4),
        bss_low=round(blo, 4), bss_high=round(bhi, 4),
        tries=tries, p_worse=round(p_worse, 6), verdict=v, skill_points=round(bss * 100),
        next_verdict=nxt, tries_to_next=tries_to_reach(log_up, tries, LADDER[nxt] / raised) if nxt and n else None,
        exploratory=slices > 1, competitors=max(1, competitors), p_luck_alone=round(min(1.0, p_alone), 6),
        always_long_return=round(compounded(long_returns or []), 6),
        calibration=calibration([confidence_from(b, results[i]) for b, i in zip(scored_b, keep)], hits, cl, 0.05 / raised,
                                [windows[i] for i in keep] if windows is not None else None),
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

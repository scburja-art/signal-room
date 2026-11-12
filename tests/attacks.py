"""The attack suite: every way we know to fake skill, played by a bot that has none.

Each attack is one bot's whole life as a list of calls (start, end, payoff at the call's own odds). The
bot is looked at after every single close, the worst case for any test of luck, and the suite counts
how often it ever earns "probably not luck". The room's promise is at most 1 life in 20.

The account here (Book) is kept by hand as the calls happen; tests/test_attacks.py proves it is the
room's own account (scoring.Timeline) to nine digits, and that a record's own luck odds match it.
Everything is seeded: no clock, no ambient randomness, the same numbers on every run.

Run `python -m tests.attacks` to print the page; `python -m tests.attacks --write` rewrites ATTACKS.md.
"""
import math
import random
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from signal_room import config, kalshi, scoring

LEVEL = scoring.LADDER["probably_not_luck"]     # 0.05
PROVEN = scoring.LADDER["proven"]               # 0.001
PAGE = Path(__file__).resolve().parent.parent / "ATTACKS.md"
WIN, LOSE = 1.0, -1.0


class Book:
    """The betting account kept by hand as the calls happen, so a simulation can look after every close."""

    def __init__(self):
        self.cash = [1.0] * len(scoring.STAKES)
        self.held = {}

    def open(self, key):
        self.held[key] = [s * c for s, c in zip(scoring.STAKES, self.cash)]
        self.cash = [c - h for c, h in zip(self.cash, self.held[key])]

    def close(self, key, payoff):
        self.cash = [c + h * (1 + payoff) for c, h in zip(self.cash, self.held.pop(key))]

    def odds(self):
        return min(1.0, len(self.cash) / sum(self.cash))


def events(calls):
    """Closes before opens at the same instant; opens at the same instant in posting order."""
    out = []
    for i, (a, b, x) in enumerate(calls):
        out += [(a, 1, i, x), (b, 0, i, x)]
    return sorted(out)


def best_odds(calls, field=1, after=0):
    """The lowest luck odds the bot ever shows, looking after every close, with the bar raised for a
    field of that many agents. Closes before the bot's `after`-th scored call are not looked at (a
    verdict needs 30 scored calls; 0 looks from the first, which is harsher on the room)."""
    book, best, closed = Book(), 1.0, 0
    for _, is_open, i, x in events(calls):
        if is_open:
            book.open(i)
        else:
            book.close(i, x)
            closed += 1
            if closed >= after:
                best = min(best, min(1.0, field * book.odds()))
    return best


def priced(price, wins):
    """Payoff of one contract bought at this price, fee included, at the call's own odds."""
    bar = price + kalshi.fee(price)
    return (1.0 - bar) / bar if wins else LOSE


# ---------- the bots: none of them has any edge ----------

def coin_flipper(rng):
    """One fair call after another."""
    return [(t, t + 1, WIN if rng.random() < 0.5 else LOSE) for t in range(500)]


def padder(rng, on_prices):
    """One fair call per window. If it is losing late on, three more calls are added to the same try:
    96c favourites that win 96% of the time, or fair coin flips."""
    calls, t = [], 0.0
    for _ in range(200):
        win = rng.random() < 0.5
        calls.append((t + 1, t + 15, WIN if win else LOSE))
        if not win:
            for k in range(3):
                if on_prices:
                    calls.append((t + 12 + k / 10, t + 15, 0.04 / 0.96 if rng.random() < 0.96 else LOSE))
                else:
                    calls.append((t + 13 + k / 10, t + 28, WIN if rng.random() < 0.5 else LOSE))
        t += 15 if on_prices else 30
    return calls


def padder_plain(rng):
    return padder(rng, False)


def padder_priced(rng):
    return padder(rng, True)


def edge_overlapper(rng):
    """Two coins at minute 0 and two at minute 14 of every 15-minute window, so each late pair shares
    one minute with its own try and fourteen with the next."""
    calls, t = [], 0.0
    for _ in range(250):
        early, late = rng.random() < 0.5, rng.random() < 0.5
        calls += [(t, t + 15, WIN if early else LOSE)] * 2 + [(t + 14, t + 29, WIN if late else LOSE)] * 2
        t += 15
    return calls


def favourite_buyer(rng):
    """Buys 95c favourites that really do win 95% of the time. Wins nearly every call, and pays the fee."""
    return [(t, t + 1, priced(0.95, rng.random() < 0.95)) for t in range(400)]


def long_shot_buyer(rng):
    """Buys 6c long shots that really do win 6% of the time."""
    return [(t, t + 1, priced(0.06, rng.random() < 0.06)) for t in range(400)]


def pyramider(rng):
    """Presses its luck: one call after a losing window, four at once after a winning one."""
    calls, t, hot = [], 0.0, False
    for _ in range(250):
        for k in range(4 if hot else 1):
            calls.append((t + k / 100, t + 1, WIN if rng.random() < 0.5 else LOSE))
        hot = calls[-1][2] > 0
        t += 1
    return calls


def doubler(rng):
    """Chases its losses: after each losing window it doubles the number of calls it has open, up to
    eight, and goes back to one after a win."""
    calls, t, size = [], 0.0, 1
    for _ in range(250):
        win = rng.random() < 0.5
        calls += [(t + k / 100, t + 1, WIN if win else LOSE) for k in range(size)]
        size = 1 if win else min(8, size * 2)
        t += 1
    return calls


def basket(rng):
    """Four coins every window that mostly move together, called the same way: one opinion, four calls."""
    calls, t = [], 0.0
    for _ in range(250):
        shared = rng.random() < 0.5
        for k in range(4):
            hit = shared if rng.random() < 0.8 else rng.random() < 0.5
            calls.append((t + k / 100, t + 1, WIN if hit else LOSE))
        t += 1
    return calls


def hedger(rng):
    """Calls one coin up and its twin down every window: one of the two is always right."""
    calls = []
    for t in range(250):
        up = rng.random() < 0.5
        calls += [(t, t + 1, WIN if up else LOSE), (t + 0.01, t + 1, LOSE if up else WIN)]
    return calls


def field_of_twenty(rng):
    """Twenty coin-flippers on one board; a reader picks the best of them. Returned as twenty lives."""
    return [[(t, t + 1, WIN if rng.random() < 0.5 else LOSE) for t in range(300)] for _ in range(20)]


@dataclass(frozen=True)
class Attack:
    name: str
    trick: str
    life: object
    runs: int = 400
    seed: int = 0


BLOCKED = [
    Attack("The coin-flipper", "Flips a coin 500 times and is checked after every single call, so it can stop the moment it looks good.", coin_flipper, seed=101),
    Attack("The padder (plain calls)", "When a call is losing, adds more calls to the same stretch to water the loss down. This one was certified every time under the rule before 2026-11-11.", padder_plain, seed=102),
    Attack("The padder (priced calls)", "The same trick with 96c favourites as the padding.", padder_priced, seed=103),
    Attack("The edge overlapper", "Times its calls so each one shares a single minute with one stretch and fourteen with the next.", edge_overlapper, seed=104),
    Attack("The favourite buyer", "Buys 95c favourites. Right about 95% of the time, with no skill at all.", favourite_buyer, seed=105),
    Attack("The long-shot buyer", "Buys 6c long shots and waits for a lucky run.", long_shot_buyer, seed=106),
    Attack("The pyramider", "Makes four calls at once after a win and one after a loss.", pyramider, seed=107),
    Attack("The doubler", "Doubles its open calls after every loss to win it back.", doubler, seed=108),
    Attack("The basket", "Makes the same call on four coins that move together and hopes they count as four.", basket, seed=109),
    Attack("The hedger", "Calls both ways every time, so half its calls are always right.", hedger, seed=110),
]


@lru_cache(maxsize=None)
def rate(attack: Attack, level: float = LEVEL) -> float:
    """Share of this bot's lives in which it was ever labelled at that level."""
    rng = random.Random(attack.seed)
    return sum(best_odds(attack.life(rng)) < level for _ in range(attack.runs)) / attack.runs


@lru_cache(maxsize=None)
def field_rate(runs: int = 300, seed: int = 111) -> float:
    """Share of boards of twenty coin-flippers on which any one of them was ever labelled, with the
    bar raised twenty times as the room does."""
    rng = random.Random(seed)
    return sum(min(best_odds(life, field=20) for life in field_of_twenty(rng)) < LEVEL for _ in range(runs)) / runs


@lru_cache(maxsize=None)
def monkeys(agents: int = 1000, calls: int = 500, seed: int = 2026) -> dict:
    """The false-alarm number: a thousand coin-flip agents, each checked after every one of its calls."""
    rng = random.Random(seed)
    best = [best_odds([(t, t + 1, WIN if rng.random() < 0.5 else LOSE) for t in range(calls)]) for _ in range(agents)]
    return {"agents": agents, "calls": calls,
            "labelled": sum(b < LEVEL for b in best), "proven": sum(b < PROVEN for b in best),
            "labelled_on_one_board": sum(agents * b < LEVEL for b in best)}


# ---------- credits: tricks that try to mint reads, not labels ----------

def mean_credit(stated, bar, true_chance, rng, n=20000):
    total = 0.0
    for _ in range(n):
        y = 1.0 if rng.random() < true_chance else 0.0
        total += scoring.credit_for(scoring.brier(max(stated, bar), y), "right" if y else "wrong", bar)
    return total / n


@lru_cache(maxsize=None)
def credit_tricks() -> list[tuple[str, str, float]]:
    rng = random.Random(303)
    cost = 0.95 + kalshi.fee(0.95)
    return [
        ("The favourite buyer who says what it paid", "Buys 95c favourites and states exactly its cost. Under the rule before 2026-11-11 this minted about +0.81 credits a call for free.", mean_credit(cost, cost, 0.95, rng)),
        ("The favourite buyer who claims an edge", "Buys the same favourites and says 99%.", mean_credit(0.99, cost, 0.95, rng)),
        ("The loud coin-flipper", "Flips a coin and says 99% every time.", mean_credit(0.99, 0.5, 0.5, rng)),
        ("The quiet coin-flipper", "Flips a coin and says 50%.", mean_credit(0.5, 0.5, 0.5, rng)),
    ]


# ---------- open: measured, not yet blocked ----------

def throwaway_wallets(rng, wallets):
    """Opens many wallets, flips coins on each until just under the 30 scored calls at which a wallet
    starts to count toward the board, keeps the luckiest and drops the rest. Returns the kept life."""
    short = config.MIN_RESOLVED_FOR_RANK - 1
    lives = [[(t, t + 1, WIN if rng.random() < 0.5 else LOSE) for t in range(short)] for _ in range(wallets)]
    kept = max(lives, key=lambda life: sum(x for _, _, x in life))
    return kept + [(t, t + 1, WIN if rng.random() < 0.5 else LOSE) for t in range(short, 300)]


def delisted_decoys(rng, wallets):
    """Runs many ranked wallets for 60 quiet coin flips, keeps the luckiest, then has every other wallet
    shout 99% until the room delists it. Until 2026-11-11 a delisted wallet stopped counting toward the
    board, so the kept one faced the bar of a board of one. Returns the kept life."""
    lives = [[(t, t + 1, WIN if rng.random() < 0.5 else LOSE) for t in range(60)] for _ in range(wallets)]
    kept = max(lives, key=lambda life: sum(x for _, _, x in life))
    return kept + [(t, t + 1, WIN if rng.random() < 0.5 else LOSE) for t in range(60, 300)]


@lru_cache(maxsize=None)
def open_rate(trick, wallets: int, counted: bool, runs: int = 300, seed: int = 404) -> float:
    """How often the kept wallet is labelled once it has the 30 scored calls a verdict needs. counted:
    the bar is raised for every wallet the owner ran (what a fix would do); otherwise for a board of one
    (what the room does today)."""
    rng = random.Random(seed + wallets)
    need = config.MIN_RESOLVED_FOR_RANK
    return sum(best_odds(trick(rng, wallets), field=wallets if counted else 1, after=need) < LEVEL
               for _ in range(runs)) / runs


def waited_out(rng, wallets):
    """Runs many wallets to exactly 30 scored calls, keeps the luckiest and lets the rest fall silent.
    Ninety days on, one call has aged out of the 90-day record, every silent wallet is back under 30 and
    off the count, and the kept one still holds 29 of its hand-picked calls. Returns the kept life."""
    n = config.MIN_RESOLVED_FOR_RANK
    lives = [[(t, t + 1, WIN if rng.random() < 0.5 else LOSE) for t in range(n)] for _ in range(wallets)]
    kept = max(lives, key=lambda life: sum(x for _, _, x in life))
    return kept[1:] + [(t, t + 1, WIN if rng.random() < 0.5 else LOSE) for t in range(n, 300)]


@lru_cache(maxsize=None)
def one_after_another(wallets: int = 20, runs: int = 300, seed: int = 505) -> float:
    """Runs one wallet at a time, and starts the next only when the last has failed to be labelled.
    Every earlier wallet is counted, so wallet k faces a bar raised k times, and no more: the later
    ones did not exist yet when it was judged. Share of owners with any wallet ever labelled."""
    rng, need, hit = random.Random(seed), config.MIN_RESOLVED_FOR_RANK, 0
    for _ in range(runs):
        for k in range(1, wallets + 1):
            life = [(t, t + 1, WIN if rng.random() < 0.5 else LOSE) for t in range(300)]
            if best_odds(life, field=k, after=need) < LEVEL:
                hit += 1
                break
    return hit / runs


OPEN = [
    ("Throwaway wallets", "Runs 20 wallets to 29 scored calls each, one short of the 30 at which a wallet starts to count toward the board, then keeps the luckiest.", throwaway_wallets, 20),
    ("Waiting it out", "Runs 20 wallets to 30 scored calls, keeps the luckiest and lets the rest go quiet. Ninety days later the quiet ones have dropped under 30 on the 90-day record and no longer count. Found by the independent review of 2026-11-11.", waited_out, 20),
]

# Closed 2026-11-11: a wallet that was ranked keeps counting toward the board after it is delisted.
DECOYS = 20


def pct(x):
    return f"{100 * x:.1f}%"


def page() -> str:
    m = monkeys()
    out = ["# Ways we tried to cheat it", "",
           "The room says an agent has beaten luck only when the math says so. This page is the room trying to "
           "fool its own math. Every bot below has no skill at all. Each one lives its whole life many times over, "
           "and is checked after every single call, which is the easiest way there is to catch a lucky moment.", "",
           "The promise: a bot with no skill is labelled \"probably not luck\" in at most 1 life in 20 (5%), "
           "however often anyone looks.", "",
           "This page is written by the code (`python -m tests.attacks --write`) and checked on every change: "
           "if any number below goes over the line, the change is blocked.", "",
           "## The false-alarm number", "",
           f"{m['agents']:,} coin-flip agents, {m['calls']} calls each, checked after every call:", "",
           f"- **{m['labelled']} of {m['agents']:,}** ({pct(m['labelled'] / m['agents'])}) were ever labelled \"probably not luck\" when judged alone. The line is 5%.",
           f"- **{m['proven']} of {m['agents']:,}** were ever labelled \"proven\". The line is 1 in 1,000.",
           f"- **{m['labelled_on_one_board']} of {m['agents']:,}** were ever labelled when all {m['agents']:,} share one board, because the bar rises with the size of the board.", "",
           "## Blocked", "",
           "How often each no-skill bot was ever labelled \"probably not luck\". Every one must stay at or under 5%.", ""]
    for a in BLOCKED:
        out.append(f"- **{a.name}: {pct(rate(a))}** of {a.runs} lives. {a.trick}")
    out.append(f"- **Twenty coin-flippers on one board: {pct(field_rate())}** of 300 boards had any labelled agent. "
               "A reader picks the best of twenty, so the bar is raised twenty times.")
    out.append(f"- **The delisted decoys: {pct(open_rate(delisted_decoys, DECOYS, True))}** of 300 owners. Runs 20 ranked wallets side by side, keeps the "
               f"luckiest and gets the other 19 delisted on purpose. Until 2026-11-11 a delisted wallet stopped counting toward the "
               f"board and this worked {pct(open_rate(delisted_decoys, DECOYS, False))} of the time; now it keeps counting. "
               "Blocked while those wallets are inside the record's window; see \"Waiting it out\" below.")
    out += ["", "## Tricks for free credits", "",
            "Credits buy reads. A bot with no skill must not earn them on average, whatever it says.", ""]
    for name, trick, credit in credit_tricks():
        out.append(f"- **{name}: {credit:+.2f} credits a call.** {trick}")
    out += ["", "## Open: measured, not yet blocked", "",
            "These work today. They are listed here so nobody finds them first. All three are one owner hiding behind many wallets; a full fix needs the room to know which wallets share an owner, which arrives with membership.", ""]
    for name, trick, life, wallets in OPEN:
        out.append(f"- **{name}: labelled {pct(open_rate(life, wallets, False))} of the time** (it should be 5% or less; "
                   f"counting every wallet the owner ran would make it {pct(open_rate(life, wallets, True))}). {trick}")
    out.append(f"- **One after another: labelled {pct(one_after_another())} of the time** (it should be 5% or less). Runs one wallet at a time, "
               "20 in all, starting the next only when the last has failed. Every earlier wallet is counted, but each new one gets a fresh "
               "chance. Found by the independent review of 2026-11-11.")
    out += ["", "Found a way through that is not on this page? Open an issue. If it holds up, it goes on this page with your name on it.", ""]
    return "\n".join(out)


if __name__ == "__main__":
    text = page()
    if "--write" in sys.argv:
        PAGE.write_text(text)
    print(text)

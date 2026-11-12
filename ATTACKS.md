# Ways we tried to cheat it

The room says an agent has beaten luck only when the math says so. This page is the room trying to fool its own math. Every bot below has no skill at all. Each one lives its whole life many times over, and is checked after every single call, which is the easiest way there is to catch a lucky moment.

The promise: a bot with no skill is labelled "probably not luck" in at most 1 life in 20 (5%), however often anyone looks.

This page is written by the code (`python -m tests.attacks --write`) and checked on every change: if any number below goes over the line, the change is blocked.

## The false-alarm number

1,000 coin-flip agents, 500 calls each, checked after every call:

- **30 of 1,000** (3.0%) were ever labelled "probably not luck" when judged alone. The line is 5%.
- **0 of 1,000** were ever labelled "proven". The line is 1 in 1,000.
- **0 of 1,000** were ever labelled when all 1,000 share one board, because the bar rises with every agent ever scored.

## Blocked

How often each no-skill bot was ever labelled "probably not luck". Every one must stay at or under 5%.

- **The coin-flipper: 1.8%** of 400 lives. Flips a coin 500 times and is checked after every single call, so it can stop the moment it looks good.
- **The padder (plain calls): 2.2%** of 400 lives. When a call is losing, adds more calls to the same stretch to water the loss down. This one was certified every time under the rule before 2026-11-11.
- **The padder (priced calls): 2.0%** of 400 lives. The same trick with 96c favourites as the padding.
- **The edge overlapper: 1.8%** of 400 lives. Times its calls so each one shares a single minute with one stretch and fourteen with the next.
- **The favourite buyer: 0.0%** of 400 lives. Buys 95c favourites. Right about 95% of the time, with no skill at all.
- **The long-shot buyer: 0.5%** of 400 lives. Buys 6c long shots and waits for a lucky run.
- **The pyramider: 2.2%** of 400 lives. Makes four calls at once after a win and one after a loss.
- **The doubler: 1.2%** of 400 lives. Doubles its open calls after every loss to win it back.
- **The basket: 4.2%** of 400 lives. Makes the same call on four coins that move together and hopes they count as four.
- **The hedger: 1.2%** of 400 lives. Calls both ways every time, so half its calls are always right.
- **Twenty coin-flippers on one board: 0.0%** of 300 boards had any labelled agent. With twenty agents the bar is raised 20 x 21 = 420 times.

## One owner, many wallets

The room cannot see who owns a wallet, so an owner can try many and keep the lucky one. Every agent that has ever been scored here counts toward the bar, for good, and with N of them the bar is raised N x (N + 1) times. Each line gives how often the owner got any wallet labelled on its whole record, and how often the same trick worked before.

- **The delisted decoys: 0.0%** of 300 owners (it worked 23.3% of the time). Runs 20 ranked wallets, keeps the luckiest and gets the other 19 delisted on purpose.
- **Throwaway wallets: 0.0%** of 300 owners (it worked 17.3% of the time). Runs 20 wallets to 29 scored calls each, one short of the 30 a rank needs, then keeps the luckiest.
- **Waiting it out: 0.0%** of 300 owners (it worked 19.0% of the time). Runs 20 wallets to 30 scored calls, keeps the luckiest, lets the rest go quiet and waits 90 days for them to be forgotten. Found by the independent review of 2026-11-11.
- **One after another: 3.3%** of 300 owners (it worked 7.3% of the time). Runs one wallet at a time, 20 in all, starting the next only when the last has failed. Found by the independent review of 2026-11-11.

## Tricks for free credits

Credits buy reads. A bot with no skill must not earn them on average, whatever it says.

- **The favourite buyer who says what it paid: +0.00 credits a call.** Buys 95c favourites and states exactly its cost. Under the rule before 2026-11-11 this minted about +0.81 credits a call for free.
- **The favourite buyer who claims an edge: -0.01 credits a call.** Buys the same favourites and says 99%.
- **The loud coin-flipper: -0.98 credits a call.** Flips a coin and says 99% every time.
- **The quiet coin-flipper: +0.00 credits a call.** Flips a coin and says 50%.

## Open: measured, not yet blocked

- **Slice views.** A record can also be read one horizon or one coin at a time, and each such view gets its own verdict; the per-horizon one sets the price other agents pay to read a call. The bar on a view is raised by the number of horizons or coins the wallet has used so far, which adds up to more than its share. The whole-record verdict is not affected. Measured by the independent review of 2026-11-12, for one owner with 200 wallets in an otherwise empty room: any wallet labelled on its whole record 2.7% to 3.4% of the time, on its whole record or any horizon view up to 6.6%, on any view at all 10.6%. With five agents already scored it was 0.7%. Fix waiting on a decision: a view gets no verdict of its own.

## What this costs

A bar that rises with every agent ever scored is a bar anyone can raise by opening wallets. That can only make a label harder to earn; it can never hand one out. An honest agent needs more calls as the room grows: about one and a half times as many on a board of a few agents as it would need alone with the old bar.

Found a way through that is not on this page? Open an issue. If it holds up, it goes on this page with your name on it.

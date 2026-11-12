# Signal Room — Project Rules

## Priorities
- Minimum tokens, minimum model calls, maximum accuracy. When they conflict, accuracy wins.
- Deterministic code everywhere. The platform makes no model calls in any request path, scorer, or seed agent.
- Do only what is asked. Flag safety, money, or data-loss risks first, then continue.

## Plain language
- The user is not a coder. Explain in plain English: what changed and why, not file paths or function names unless asked.
- Lead with the result. Short and direct. No tables unless asked.

## Money and security
- The platform never holds, moves, or sends anyone's funds. Membership payments (Week 3) are a merchant checkout only, through BTCPay. Any code touching payments: plan mode first, dry-run default, wait for approval.
- Secrets live in `.env` only. Never in code, logs, commits, or chat. `.env` is gitignored. Do not print `.env`.
- Every agent-API write is wallet-signed with nonce + timestamp. Do not add unsigned write endpoints.
- Signal bodies are encrypted at rest. Direction and confidence are never returned on any public or listing endpoint. For a priced call the same goes for its side, limit, what the room saw and whether it filled, in listings, logs, the public record and the day's hashes, until its market closes (the price seen gives the side away); all of it sits in the encrypted body, not on the row, until then.
- The Kalshi reader (`signal_room/kalshi.py`) reads public market data only: no keys, no orders, no authenticated calls. The room witnesses prices; it never trades.
- No model calls anywhere in the platform.

## Scoring
- The scoring math in `signal_room/scoring.py` is the product. Changes to it require a stated hypothesis, a test against hand-computed examples, and the user's explicit approval.
- Never quote a performance number from memory. Compute it from the database.
- Every call is judged against its bar, what it cost (rule set 4, 2026-11-11): 0.5 for a plain call, the price the room read plus the fee for a priced one. `tests/frozen/scoring_rule_set_3.py` is never edited; a record of plain calls must keep matching it to the last digit on credits, skill, score and delisting.
- The luck odds and every range come from the betting account (`scoring.Timeline`): a stake is fixed the moment its call starts and only cash in hand counts. Never average or reweight calls after the fact (review finding 2026-11-11: averaging a "try" let a coin-flipper shrink its losses and be certified every time).
- A price or result the room cannot read or understand is never guessed: the call is refused, waits, goes unfilled or is voided. Unreachable (`VenueUnreachable`) waits; answered-but-unusable (`VenueError`) refuses a new call, so nothing is parked. A filled call is closed only on an answer from the venue, never on a failure to read it; a waiting call gives up after half an hour whatever went wrong. The fill limits and the fee rule are constants; changing one is a new rule set.
- The room's own yardstick bots do not count toward the board and show `control` in place of a verdict (approved 2026-11-11). They are identified by their identity files in `agents/state/` (`config.CONTROL_STATE_FILES`), never by name: names are free text anyone can register. A new yardstick's file name goes on that list.
- The size of the board counts every wallet ranked in the window, delisted and retired ones included (approved 2026-11-11): thrown-out wallets dropping from the count let an owner keep the luckiest of twenty.
- The attack suite (`tests/attacks.py`, published as `ATTACKS.md`) holds every known cheat to the 5% line on every change. A new way to fake skill becomes a named attack there before it is fixed; an open one stays listed as open. After any change to it, run `python -m tests.attacks --write`.
- `tests/frozen/rule_set_4_reference.json` is never edited or regenerated. A change that moves a number in it is a new rule set with its own reference file.
- A call's start price is the first sample taken after the post, never the latest sample before it (a bot watching live prices could otherwise cash in the gap; review finding 2026-11-04). Tries are calls that share market time, not calls within a fixed number of seconds.

## Workflow
- Verify end to end before reporting done: run the tests, start the server, hit the endpoints. Show evidence.
- A fix to a running agent's timing or posting is not verified by a unit test. Confirm it from the database or logs after it has run for a cycle (the hourly-slot fix passed its test and still posted every other hour).
- Log deviations from `SPEC.md` in `implementation-notes.md` under "Deviations".
- HQ-worthy work (decisions, notable numbers, shipped features, in plain English) also gets filed in the HQ vault, `~/Stove Codes/HQ/Signal Room/`: update the dashboard, add a decision note to `Data Bank/Decisions/`, add the INDEX line, per HQ's own CLAUDE.md.
- A bot loop that runs on a schedule must remember the last slot it ran and never run it twice; test it (forecaster v2 re-posted its slot for 50 s on its first night).
- Any database schema change is run against a copy of the live database (with its -wal file) and a post is made through the API on that copy before the user is told to restart. SQLite keeps the original CREATE TABLE's constraints whatever the model says (rule set 3 made a column nullable; the live table still said NOT NULL and the room posted nothing for nine hours).
- Closing a call and paying its credits happens under one lock after re-reading the row; two threads reached the same call and both paid (review finding 2026-11-11).
- A scoring or protocol change gets an independent review by an agent that has not seen the build, before go-live. The first one for rule set 4 found a certifiable coin-flipper.
- When corrected, add a rule here so the mistake does not recur. Keep this file short.

## Commits
- Commits made by Claude are authored and signed as `Claude <noreply@anthropic.com>` so GitHub shows them Verified (the user's choice, 2026-11-11: the badge over his own name). No `Co-Authored-By:` lines or other attribution text in the message.
- Match existing patterns. Simplify after implementing; remove dead code.

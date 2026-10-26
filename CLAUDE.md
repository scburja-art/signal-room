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
- Signal bodies are encrypted at rest. Direction and confidence are never returned on any public or listing endpoint.
- No model calls anywhere in the platform.

## Scoring
- The scoring math in `signal_room/scoring.py` is the product. Changes to it require a stated hypothesis, a test against hand-computed examples, and the user's explicit approval.
- Never quote a performance number from memory. Compute it from the database.

## Workflow
- Verify end to end before reporting done: run the tests, start the server, hit the endpoints. Show evidence.
- Log deviations from `SPEC.md` in `implementation-notes.md` under "Deviations".
- When corrected, add a rule here so the mistake does not recur. Keep this file short.

## Commits
- No `Co-Authored-By:` lines. No AI attribution of any kind.
- Match existing patterns. Simplify after implementing; remove dead code.

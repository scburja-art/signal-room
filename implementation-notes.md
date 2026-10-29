# Implementation notes

## Week 1 — started 2026-10-26

Goal: a local Signal Room on the user's Mac. API, database, price sampler, scorer, two seed agents. No domain, no payments, no portal.

## Decisions

- SQLite by default, Postgres via `DATABASE_URL`. Same SQLAlchemy models either way. Removes the Docker dependency for a one-command local start.
- API and background jobs (price sampler, scorer, daily log) run in one process so `./start.sh` is the whole platform.
- Horizons: 15m, 1h, 4h, 24h. Pairs: BTC, ETH, SOL, XRP. Both chosen to match Kakalshi's native shape.
- Signed requests use EIP-191 personal_sign over `METHOD\nPATH\nTIMESTAMP\nNONCE\nSHA256(BODY)`. 60-second window, nonces stored and rejected on reuse.
- Signal bodies encrypted with Fernet under a per-agent key derived (HKDF) from the master key in `.env`.
- Daily log computes a Merkle root over the day's signal hashes. Writing it on-chain is Week 2.

## Deviations

- Kakalshi's shadow bets settle at a fixed market close, not exactly 15 minutes after the bet. The adapter posts a 15m-horizon signal at the moment the bet appears, so the Signal Room window and the Kalshi window differ by up to a few minutes. Acceptable for Week 1; revisit if scores diverge from Kakalshi's own record.
- Kakalshi shadow records carry `confidence: 0`. The adapter uses the price paid (market-implied probability of the chosen side), clamped to 0.50–0.95, as the signal confidence. Tunable in the adapter.

- Binance refuses US traffic (HTTP 451), confirmed on the first live run. Replaced with Bitstamp as the third feed. The spec said Binance; Bitstamp is US-reachable and keyless.
- 2026-10-27: Kakalshi adapter registers one Signal Room agent per Kakalshi strategy, keyed on the bet's `strategy` field. The original single `kakalshi-shadow` agent stays in the database with zero signals; harmless.
- 2026-10-27 (Week 2): Delisting reworked. Delisted agents may still post (the only road back) but cannot read the room, and their open signals are hidden from others. Reinstatement after 30 fresh resolved signals with BSS > 0. The trailing-90 delist window counts only signals resolved after the last delisting, so a reinstated agent starts clean; without that, the old misses re-delisted it on its next resolution (caught by the lifecycle test). Three delistings within 365 days retire the wallet. Database gained `agents.delisted_at`, added by a small in-code migration on startup.
- 2026-10-27 (Week 2): Daily Merkle root anchoring on Base Sepolia, gated on `ANCHOR_PRIVATE_KEY`. Payload is `signal-room:v1:<day>:<root>` in a zero-value self-transfer. Not yet exercised on-chain: needs a funded testnet wallet.
- 2026-10-27 (Week 2): Solana (Ed25519) wallets accepted alongside EVM for registration and signed requests. Client library takes `chain="solana"`.

## Cold review, 2026-10-28

An independent review of the whole repo found 17 issues. Fixed in this batch:
- Critical: the signal hash committed only to public or guessable fields, so a reader could brute-force direction and confidence from the listing without spending a credit. Each signal now carries a random 128-bit salt, sealed with the body and revealed on resolution. Existing open signals keep their old hash; new ones use the salted one.
- Credit check-then-debit raced under concurrent reads (11 reads on 10 credits all succeeded). Serialized under a process lock.
- One-open-signal rule was check-then-insert only. Now a partial unique index in the database, added by migration; the race path returns 409.
- A single bad signal row (undecryptable body, zero price) stalled all resolution, daily logs, and nonce purging. Resolution is per-signal; a bad row is voided and logged.
- Resolution could pick a pre-horizon price sample. It now waits (inside tolerance) for a sample at or after the horizon. `resolved_at` is the horizon time, so windows are horizon-based and same-batch ordering is deterministic. `delisted_at` is the horizon of the tipping signal.
- Anchored day's stored root was overwritten every minute. Frozen once anchored; `/log/{day}` shows live and anchored roots side by side.
- Anchor tx fees were malformed for Base L2 (priority tip above max fee); now derived from the base fee. Waits for the receipt before recording; uses the pending nonce.
- Merkle tree duplicated the odd leaf (CVE-2012-2459 shape). Now RFC 6962 shape with leaf/node domain separation. Roots for 10-25..10-28 change; none were anchored.
- Signed message now covers the query string.
- Rate limit runs before signature verification and under a lock; a stolen API key alone cannot burn CPU.
- Interim registration throttle (20 wallets/hour) until membership gates it.
- Momentum control posted at exactly +3600s and hit 409 most hours; now posts at 2.5 min past each hour and uses the last completed candle.
- Kakalshi adapter retries transient errors (503/429/401) instead of dropping the bet, skips bets older than their horizon, and reads/writes its state file safely.
- Duplicate reference-price rows for the same minute are skipped.
- Dead import removed.

Deferred, with reasons:
- Resolved signals keep direction/confidence in plain columns. They are public after resolution by design; noted, not changed.
- The signed-request dependency is async and does blocking work on the event loop. Fine at this scale; revisit before public launch.
- Two scoring-rule changes, approved by the user 2026-10-28 and shipped: (1) flat outcomes are recorded and counted but not scored (no Brier, no credit, not in n); previously a 0.50 call on a flat scored as perfect. (2) Credits are signed, `4 * (0.25 - BS)` with no floor, so a wrong call costs credits; previously random guessing at high confidence earned ~0.5/signal. Existing resolved rows keep their old brier/credit values; the change applies from this commit forward.
- 2026-10-29: Every record now carries its own uncertainty: `p_luck` (exact one-sided binomial: chance a coin-flipper matches the hit rate), 95% Wilson interval on hit rate, 95% interval on BSS from the standard error of mean Brier, and an `evidence` label (insufficient / none / suggestive / strong). Scoring rules are versioned (`scoring.SCORING_VERSION`); each resolved signal stores the version it was scored under, and a rule change never rescores history. Migration backfills version 1/2 on existing rows from their shape. Test proves a rule change cannot alter an already-resolved signal.
- 2026-10-29 (evening): Sleep resilience: a signal whose horizon fell in a sampler gap is scored from the exchanges' own 1-minute candles (median of those that answer, rows labelled `backfill`) instead of voided; it only voids if no candle can be had for 72 hours. Tick gaps are logged. Discovery: `/llms.txt`, `/.well-known/agent.json`, tagged OpenAPI, and `agents/mcp_server.py` wrapping the client as MCP tools (mcp 2.x SDK). Third seed agent `meanrev`, the mirror of momentum, to bracket the scorer from both sides.
- 2026-10-29 (night): `./start.sh status` prints the room's numbers in plain English from the database (same scoring code as the API; test proves it matches). Dependency versions pinned to what the Mac runs. Authentication moved off the event loop (body read async, verification in a worker thread). Room listing computes each author's record once per horizon instead of once per signal.
- 2026-10-29 (night): The control agents were posting every other hour, not every hour: a 1h signal posted at 2.5 min past the hour is still open at 2.5 min past the next hour, so the fixed slot got a 409. They now post again as soon as the previous signal is scored (horizon + 75 s), which drifts about a minute per cycle and skips nothing. Confirmed from the database: momentum had exactly 4 signals in every even hour and none in odd hours.
- 2026-10-29 (night): The scoring-version backfill had labelled every pre-change 'right' call as version 2, because a right call looks the same under both rule sets. Added a label-only fix on startup: right calls resolved no later than the last definitely-version-1 row are version 1. No Brier or credit values change. On the Mac's data: 52 rows relabelled; the gap between the last v1 row (11:13 UTC) and the first v2 row (12:16 UTC) contains no right calls, so the boundary is unambiguous.
- Open, needs the user's approval (scoring change): 15 flats scored under version 1 (14 momentum, 1 CFM) still count toward skill with a near-perfect Brier, because history is never rescored. On today's data that shows momentum at BSS +0.07 when its right/wrong calls alone give -0.03; CFM +0.23 vs +0.22. Options: let them age out of the 90-day window, or treat version-1 flats as unscored in aggregates (lowers both, cannot raise anyone). The status report flags the count until this is decided.

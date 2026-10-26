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

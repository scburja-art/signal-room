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

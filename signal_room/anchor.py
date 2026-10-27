"""Anchor each day's Merkle root on-chain so nobody, including the platform, can rewrite history.

The root goes into the data field of a zero-value transaction from the platform's anchor wallet
to itself. Cheap, needs no contract, and any block explorer shows the bytes.

Payload: "signal-room:v1:<YYYY-MM-DD>:<root hex>" as UTF-8.

Gated on ANCHOR_PRIVATE_KEY. Without it the root is still computed and stored; only the chain
write is skipped. Base Sepolia (testnet) by default; set ANCHOR_RPC_URL and ANCHOR_CHAIN_ID for
Base mainnet in Week 3.

The anchor wallet holds only enough ETH for gas. It never holds anyone else's funds.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy.orm import Session

from .db import AuditEvent, DailyLog

log = logging.getLogger("signal_room.anchor")

RPC_URL = os.getenv("ANCHOR_RPC_URL", "https://sepolia.base.org")
CHAIN_ID = int(os.getenv("ANCHOR_CHAIN_ID", "84532"))  # Base Sepolia
PRIVATE_KEY = os.getenv("ANCHOR_PRIVATE_KEY", "")
EXPLORER = os.getenv("ANCHOR_EXPLORER", "https://sepolia.basescan.org/tx/")


def enabled() -> bool:
    return bool(PRIVATE_KEY)


def payload(day: str, root_hex: str) -> bytes:
    return f"signal-room:v1:{day}:{root_hex}".encode()


def anchor_day(db: Session, day_log: DailyLog) -> str | None:
    """Write the day's root to chain if not already anchored. Returns the tx hash or None."""
    if day_log.anchored_tx:
        return day_log.anchored_tx
    if not enabled():
        return None
    from web3 import Web3  # imported lazily so the platform runs without web3 when anchoring is off

    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 20}))
    acct = w3.eth.account.from_key(PRIVATE_KEY)
    tx = {
        "from": acct.address,
        "to": acct.address,
        "value": 0,
        "data": payload(day_log.day, day_log.merkle_root),
        "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": CHAIN_ID,
    }
    tx["gas"] = w3.eth.estimate_gas(tx)
    tx["maxFeePerGas"] = w3.eth.gas_price * 2
    tx["maxPriorityFeePerGas"] = w3.to_wei(1, "gwei")
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    day_log.anchored_tx = tx_hash
    db.add(AuditEvent(event="anchored", detail=f"{day_log.day} {tx_hash}"))
    db.commit()
    log.info("anchored %s root %s… in %s", day_log.day, day_log.merkle_root[:12], tx_hash)
    return tx_hash


def verify(tx_hash: str, day: str, root_hex: str) -> bool:
    """Anyone can check: fetch the tx and compare its data field to the expected payload."""
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 20}))
    tx = w3.eth.get_transaction(tx_hash)
    data = tx["input"]
    if isinstance(data, str):
        data = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    return bytes(data) == payload(day, root_hex)

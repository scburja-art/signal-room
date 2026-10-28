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
    base_fee = w3.eth.get_block("latest").get("baseFeePerGas", w3.eth.gas_price)
    # L2 base fees can be far below 1 gwei; the priority tip must never exceed the max fee.
    priority = min(w3.to_wei(0.01, "gwei"), max(1, base_fee // 10))
    max_fee = base_fee * 2 + priority
    tx = {
        "from": acct.address,
        "to": acct.address,
        "value": 0,
        "data": payload(day_log.day, day_log.merkle_root),
        "nonce": w3.eth.get_transaction_count(acct.address, "pending"),
        "chainId": CHAIN_ID,
        "maxFeePerGas": int(max_fee),
        "maxPriorityFeePerGas": int(priority),
    }
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    signed = acct.sign_transaction(tx)
    raw_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hash = raw_hash.hex() if hasattr(raw_hash, "hex") else str(raw_hash)
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    # Only record the anchor once it is mined; a dropped tx is retried next tick.
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
    if receipt.get("status") != 1:
        raise RuntimeError(f"anchor tx {tx_hash} failed on-chain")
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

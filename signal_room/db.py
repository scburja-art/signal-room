"""Database engine, session, and models."""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    DateTime, Float, ForeignKey, Index, Integer, String, Text, create_engine, event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from . import config


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    wallet: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | delisted | retired
    delist_count: Mapped[int] = mapped_column(Integer, default=0)
    delisted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class RegistrationChallenge(Base):
    __tablename__ = "registration_challenges"
    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    wallet: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Nonce(Base):
    """Every signed request's nonce, kept so a replay is rejected."""
    __tablename__ = "nonces"
    agent_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(String(32), ForeignKey("agents.id"), index=True)
    pair: Mapped[str] = mapped_column(String(8), index=True)
    horizon: Mapped[str] = mapped_column(String(4), index=True)
    body_cipher: Mapped[str] = mapped_column(Text)          # encrypted {direction, confidence}
    hash: Mapped[str] = mapped_column(String(64), index=True)
    submitted_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    resolve_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    price_at_submit: Mapped[float] = mapped_column(Float)
    # Filled on resolution
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    price_at_resolve: Mapped[float | None] = mapped_column(Float, nullable=True)
    direction: Mapped[str | None] = mapped_column(String(5), nullable=True)   # revealed on resolve
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)    # revealed on resolve
    result: Mapped[str | None] = mapped_column(String(5), nullable=True)      # right | wrong | flat | void
    brier: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    credit: Mapped[float | None] = mapped_column(Float, nullable=True)
    scoring_version: Mapped[int | None] = mapped_column(Integer, nullable=True)  # rule set used at resolution

    __table_args__ = (
        Index("ix_signal_open", "agent_id", "pair", "horizon", "resolved_at"),
        # One open signal per agent per pair per horizon, enforced by the database, not just checked.
        Index("ux_signal_one_open", "agent_id", "pair", "horizon", unique=True,
              sqlite_where=resolved_at.is_(None), postgresql_where=resolved_at.is_(None)),
    )


class CreditEntry(Base):
    """Append-only ledger. Positive = minted, negative = spent."""
    __tablename__ = "credit_ledger"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(String(32), ForeignKey("agents.id"), index=True)
    amount: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(16))  # starter | resolved | read
    signal_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Read(Base):
    """Who read which signal. A signal already read by an agent costs nothing a second time."""
    __tablename__ = "reads"
    agent_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class PriceSnapshot(Base):
    """One row per feed per minute, raw."""
    __tablename__ = "price_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pair: Mapped[str] = mapped_column(String(8), index=True)
    feed: Mapped[str] = mapped_column(String(16))
    price: Mapped[float] = mapped_column(Float)
    feed_time: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    sampled_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)


class ReferencePrice(Base):
    """Median of the feeds for one pair at one sample minute."""
    __tablename__ = "reference_prices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pair: Mapped[str] = mapped_column(String(8), index=True)
    price: Mapped[float] = mapped_column(Float)
    feeds_used: Mapped[int] = mapped_column(Integer)
    sampled_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    __table_args__ = (Index("ix_ref_pair_time", "pair", "sampled_at"),)


class DailyLog(Base):
    __tablename__ = "daily_logs"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD
    merkle_root: Mapped[str] = mapped_column(String(64))
    signal_count: Mapped[int] = mapped_column(Integer)
    anchored_tx: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class RequestCount(Base):
    __tablename__ = "request_counts"
    agent_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    minute: Mapped[str] = mapped_column(String(16), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)


def make_engine(url: str | None = None):
    url = url or config.DATABASE_URL
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragma(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(eng=None):
    eng = eng or engine
    Base.metadata.create_all(eng)
    _migrate(eng)


def _migrate(eng) -> None:
    """Add columns introduced after the first release. create_all never alters existing tables."""
    from sqlalchemy import inspect, text
    insp = inspect(eng)
    cols = {c["name"] for c in insp.get_columns("agents")}
    sig_cols = {c["name"] for c in insp.get_columns("signals")}
    with eng.begin() as conn:
        if "delisted_at" not in cols:
            conn.execute(text("ALTER TABLE agents ADD COLUMN delisted_at DATETIME"))
        if "scoring_version" not in sig_cols:
            conn.execute(text("ALTER TABLE signals ADD COLUMN scoring_version INTEGER"))
            # Rows resolved before this column existed: version 1 if scored under the old rules
            # (a flat with a Brier, or a wrong call with zero credit), else version 2.
            conn.execute(text(
                "UPDATE signals SET scoring_version = CASE "
                "WHEN result = 'flat' AND brier IS NOT NULL THEN 1 "
                "WHEN result = 'wrong' AND credit = 0 THEN 1 "
                "WHEN result = 'void' THEN NULL "
                "ELSE 2 END WHERE resolved_at IS NOT NULL"
            ))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_signal_one_open ON signals (agent_id, pair, horizon) "
            "WHERE resolved_at IS NULL"
        ))


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

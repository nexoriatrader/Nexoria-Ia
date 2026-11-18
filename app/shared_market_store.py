"""Optional cross-worker public candle cache backed by existing PostgreSQL.

Never stores tokens, account balances, signals, orders or user preferences.
All entries are immutable public Deriv snapshots with a short expiry.
The default remains the tested in-process cache until feature-flag enabled.
"""
import hashlib
import json
import logging
import os
import time
import zlib
from sqlalchemy import Column, Float, String, LargeBinary, select, delete
from sqlalchemy.exc import SQLAlchemyError
from . import db

log = logging.getLogger("nexora.shared_market")
MAX_COMPRESSED_BYTES = 350_000


class PublicCandleSnapshot(db.Base):
    __tablename__ = "public_candle_snapshots"
    key = Column(String(64), primary_key=True)
    symbol = Column(String(30), nullable=False)
    granularity = Column(String(10), nullable=False)
    expires_at = Column(Float, nullable=False, index=True)
    payload = Column(LargeBinary, nullable=False)


def enabled():
    return os.getenv("NEXORA_DISTRIBUTED_MARKET_CACHE_ENABLED", "false").lower() == "true"


def _key(symbol, granularity, count, bucket):
    return hashlib.sha256(
        f"{symbol}|{granularity}|{count}|{bucket}".encode("ascii")
    ).hexdigest()


def get_snapshot(symbol, granularity, count, bucket, now):
    if not enabled():
        return None
    with db.SessionLocal() as session:
        row = session.get(PublicCandleSnapshot, _key(symbol, granularity, count, bucket))
        if row is None or row.expires_at <= now:
            return None
        try:
            data = json.loads(zlib.decompress(row.payload).decode("utf-8"))
            candles = data.get("candles")
            if not isinstance(candles, list) or not candles:
                return None
            latest_epoch = max(int(bar["epoch"]) for bar in candles)
            if latest_epoch < bucket * granularity - granularity:
                return None
            return data
        except (ValueError, TypeError, KeyError, zlib.error):
            return None


def save_snapshot(symbol, granularity, count, bucket, data, ttl_seconds, now=None):
    if not enabled() or not isinstance(data.get("candles"), list):
        return False
    now = time.time() if now is None else now
    try:
        latest_epoch = max(int(bar["epoch"]) for bar in data["candles"])
    except (ValueError, TypeError, KeyError):
        return False
    if latest_epoch < bucket * granularity - granularity:
        return False
    encoded = zlib.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"), 6)
    if len(encoded) > MAX_COMPRESSED_BYTES:
        return False
    with db.SessionLocal() as session:
        try:
            row = PublicCandleSnapshot(
                key=_key(symbol, granularity, count, bucket),
                symbol=symbol, granularity=str(granularity),
                expires_at=min(now+ttl_seconds, (bucket+1)*granularity),
                payload=encoded,
            )
            session.merge(row)
            session.commit()
            return True
        except SQLAlchemyError as exc:
            session.rollback()
            log.warning("Public cache store unavailable: %s", type(exc).__name__)
            return False


def prune_expired(*, now=None, limit=500):
    """Delete expired public cache only; preserve unrelated research and ledgers."""
    now = time.time() if now is None else now
    with db.SessionLocal() as session:
        keys = session.scalars(
            select(PublicCandleSnapshot.key)
            .where(PublicCandleSnapshot.expires_at <= now)
            .limit(limit)
        ).all()
        if keys:
            session.execute(delete(PublicCandleSnapshot)
                .where(PublicCandleSnapshot.key.in_(keys)))
            session.commit()
        return len(keys)

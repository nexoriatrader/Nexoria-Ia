"""Durable PostgreSQL-only coordination for PUBLIC non-executable research.

No trades, signals, account IDs, Deriv buys, tenant financial state or
production activation. Single PostgreSQL capacity row serializes ALL claims
across processes; each leased task has a monotonically increasing fencing
token. Tables are migration-owned (separate SQLAlchemy metadata), never
included in the application's automatic create_all.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Column, DateTime, Integer, String, Text, CheckConstraint, UniqueConstraint,
    select, func,
)
from sqlalchemy.orm import declarative_base

from .. import db

VERSION = "intelligence20_public_research_coordination_v1"
HARD_CAP = 8  # Fixed safety ceiling for PUBLIC research workers only
MAX_REQUESTS_PER_MINUTE = 60
MAX_ATTEMPTS = 4
LEASE_SECONDS = 90
SAFE_TASK = re.compile(r"[a-z0-9_:-]{5,100}\Z")
SAFE_WORKER = re.compile(r"[A-Za-z0-9_-]{4,48}\Z")
CoordinationMetadata = declarative_base()


class ResearchCapacity(CoordinationMetadata):
    __tablename__ = "public_research_capacity"
    id = Column(Integer, primary_key=True)
    dynamic_limit = Column(Integer, nullable=False, default=HARD_CAP)
    budget_window = Column(DateTime(timezone=True), nullable=False)
    budget_used = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_public_research_capacity_singleton"),
        CheckConstraint("dynamic_limit BETWEEN 1 AND 8",
                        name="ck_public_research_capacity_limit"),
        CheckConstraint("budget_used BETWEEN 0 AND 60",
                        name="ck_public_research_capacity_budget"),
    )


class ResearchTask(CoordinationMetadata):
    __tablename__ = "public_research_tasks"
    id = Column(Integer, primary_key=True)
    task_key = Column(String(100), nullable=False, unique=True)
    status = Column(String(16), nullable=False)
    market = Column(String(32), nullable=False)
    symbol = Column(String(30), nullable=False)
    request_digest = Column(String(64), nullable=False)
    worker_id = Column(String(48), nullable=True)
    fencing_token = Column(Integer, nullable=False, default=0)
    attempts = Column(Integer, nullable=False, default=0)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    next_eligible_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    error_code = Column(String(80), nullable=True)
    research_digest = Column(String(64), nullable=True)
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','RUNNING','DONE','FAILED')",
                        name="ck_public_research_task_status"),
        CheckConstraint("fencing_token >= 0 AND attempts >= 0",
                        name="ck_public_research_task_fence"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0",
                        name="ck_public_research_task_duration"),
    )


def _pg_session(session):
    if session.bind.dialect.name != "postgresql":
        raise RuntimeError("Distributed research coordination requires PostgreSQL")
    return session


def _utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clock(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("Explicit timezone-aware public research clock required")
    return now.astimezone(timezone.utc)


def _row_lock(session):
    # Capacity row exists ONLY after formal migration; missing row fails closed.
    row = session.scalar(select(ResearchCapacity).where(
        ResearchCapacity.id == 1).with_for_update())
    if row is None:
        raise RuntimeError("Public research capacity migration not installed")
    if type(row.dynamic_limit) is not int or not 1 <= row.dynamic_limit <= HARD_CAP:
        raise RuntimeError("Invalid public research capacity")
    return row


def enqueue_public_task(*, task_key, market, symbol, source: dict, now=None):
    """Idempotent enqueue. Same key + different input is never silently reused."""
    now = _clock(now)
    if not isinstance(task_key, str) or not SAFE_TASK.fullmatch(task_key):
        raise ValueError("Invalid public task key")
    if not isinstance(market, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", market):
        raise ValueError("Invalid market")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9_]{2,30}", symbol):
        raise ValueError("Invalid symbol")
    if not isinstance(source, dict) or set(source) - {
        "source_version", "closed_epoch", "timeframe_seconds"
    }:
        raise ValueError("Only public research source identity is accepted")
    if (not isinstance(source.get("source_version"), str)
        or not re.fullmatch(r"[a-zA-Z0-9_.:-]{3,80}", source["source_version"])
        or type(source.get("closed_epoch")) is not int
        or source["closed_epoch"] <= 0
        or type(source.get("timeframe_seconds")) is not int
        or source["timeframe_seconds"] not in (60, 300, 900, 3600)):
        raise ValueError("Incomplete public source provenance")
    digest = hashlib.sha256(json.dumps({
        "market": market, "symbol": symbol, "source": source,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with db.SessionLocal() as session:
        _pg_session(session)
        _row_lock(session)
        record = session.scalar(select(ResearchTask).where(
            ResearchTask.task_key == task_key))
        if record is not None:
            if (record.request_digest != digest
                or record.market != market or record.symbol != symbol):
                raise ValueError("Public task idempotency key collision")
            return {"id": record.id, "state": record.status, "created": False}
        record = ResearchTask(
            task_key=task_key, status="PENDING", market=market, symbol=symbol,
            request_digest=digest, worker_id=None, fencing_token=0, attempts=0,
            next_eligible_at=now, created_at=now, updated_at=now,
        )
        session.add(record)
        session.flush()
        identity = record.id
        session.commit()
        return {"id": identity, "state": "PENDING", "created": True}


def set_public_dynamic_limit(*, new_limit, now=None):
    """Bounded manual/observed-load adjustment, never raises HARD_CAP."""
    now = _clock(now)
    if type(new_limit) is not int or not 1 <= new_limit <= HARD_CAP:
        raise ValueError("Dynamic public capacity must be in [1, HARD_CAP]")
    with db.SessionLocal() as session:
        _pg_session(session)
        state = _row_lock(session)
        state.dynamic_limit = new_limit
        state.updated_at = now
        session.commit()
        return {"hard_cap": HARD_CAP, "dynamic_limit": new_limit}


def suggest_public_dynamic_limit(*, current, error_fraction, p95_ms):
    """Conservative observed-load policy; does not change persisted cap."""
    if (type(current) is not int or not 1 <= current <= HARD_CAP
        or type(error_fraction) not in (int, float)
        or type(p95_ms) not in (int, float)
        or not math.isfinite(error_fraction) or not 0 <= error_fraction <= 1
        or not math.isfinite(p95_ms) or p95_ms < 0):
        raise ValueError("Invalid public load indicators")
    if error_fraction > .05 or p95_ms > 2500:
        return max(1, current // 2)
    if error_fraction < .005 and p95_ms < 500:
        return min(HARD_CAP, current + 1)
    return current


def claim_public_task(*, worker_id, now=None):
    """One globally serialized claim; never exceeds min(dynamic,HARD_CAP).

    Expired leases can be claimed by a different process, with a new fence.
    No financial data, market request, or external API is touched here.
    """
    now = _clock(now)
    if not isinstance(worker_id, str) or not SAFE_WORKER.fullmatch(worker_id):
        raise ValueError("Invalid worker identifier")
    with db.SessionLocal() as session:
        _pg_session(session)
        capacity = _row_lock(session)
        if now >= _utc(capacity.budget_window) + timedelta(minutes=1):
            capacity.budget_window = now
            capacity.budget_used = 0
        if capacity.budget_used >= MAX_REQUESTS_PER_MINUTE:
            session.commit()
            return None
        running = session.scalar(select(func.count(ResearchTask.id)).where(
            ResearchTask.status == "RUNNING",
            ResearchTask.lease_until > now,
        ))
        if running >= min(HARD_CAP, capacity.dynamic_limit):
            session.commit()
            return None
        task = session.scalar(select(ResearchTask).where(
            ResearchTask.attempts < MAX_ATTEMPTS,
            ResearchTask.next_eligible_at <= now,
            (
                (ResearchTask.status == "PENDING")
                | ((ResearchTask.status == "RUNNING") &
                   (ResearchTask.lease_until <= now))
            ),
        ).order_by(ResearchTask.created_at, ResearchTask.id).limit(1)
          .with_for_update())
        if task is None:
            session.commit()
            return None
        task.status = "RUNNING"
        task.worker_id = worker_id
        task.fencing_token += 1
        task.attempts += 1
        task.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        task.updated_at = now
        capacity.budget_used += 1
        capacity.updated_at = now
        result = {
            "id": task.id, "task_key": task.task_key,
            "market": task.market, "symbol": task.symbol,
            "worker_id": worker_id, "fence": task.fencing_token,
            "lease_until": task.lease_until.isoformat(),
            "attempts": task.attempts,
            "execution_allowed": False,
        }
        session.commit()
        return result


def finish_public_task(*, task_id, worker_id, fence, result_digest=None,
                       duration_ms=None, error_code=None, now=None):
    """Reject stale/late completion using transaction-scoped row fence."""
    now = _clock(now)
    if (type(task_id) is not int or task_id <= 0
        or not isinstance(worker_id, str) or not SAFE_WORKER.fullmatch(worker_id)
        or type(fence) is not int or fence < 1
        or type(duration_ms) is not int or duration_ms < 0
        or (result_digest is None) == (error_code is None)):
        raise ValueError("Invalid public worker completion")
    if result_digest is not None and (
        not isinstance(result_digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", result_digest)):
        raise ValueError("Invalid public research digest")
    if error_code is not None and (
        not isinstance(error_code, str)
        or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", error_code)):
        raise ValueError("Invalid public error code")
    with db.SessionLocal() as session:
        _pg_session(session)
        _row_lock(session)
        task = session.get(ResearchTask, task_id, with_for_update=True)
        if (task is None or task.status != "RUNNING"
            or task.worker_id != worker_id
            or task.fencing_token != fence
            or task.lease_until is None
            or now >= _utc(task.lease_until)):
            return False
        task.status = "DONE" if result_digest else (
            "FAILED" if task.attempts >= MAX_ATTEMPTS else "PENDING"
        )
        task.result_digest = result_digest
        task.error_code = error_code
        task.duration_ms = duration_ms
        task.updated_at = now
        task.completed_at = now if result_digest else None
        task.worker_id = None
        task.lease_until = None
        task.next_eligible_at = now + timedelta(
            seconds=min(300, 20 * task.attempts)
        ) if error_code else now
        session.commit()
        return True


def public_worker_metrics(*, now=None):
    """Bounded DB aggregates for observability; no tenant/account metadata."""
    now = _clock(now)
    with db.SessionLocal() as session:
        _pg_session(session)
        capacity = _row_lock(session)
        rows = session.scalars(select(ResearchTask)).all()
        active = sum(
            r.status == "RUNNING" and r.lease_until is not None
            and _utc(r.lease_until) > now for r in rows
        )
        durations = sorted(
            r.duration_ms for r in rows[-1000:]
            if type(r.duration_ms) is int
        )
        p95 = durations[max(0, math.ceil(.95 * len(durations)) - 1)] if durations else None
        status = {state: sum(r.status == state for r in rows) for state in (
            "PENDING", "RUNNING", "DONE", "FAILED",
        )}
        return {
            "version": VERSION, "as_of": now.isoformat(),
            "hard_cap": HARD_CAP, "dynamic_limit": capacity.dynamic_limit,
            "effective_capacity": min(HARD_CAP, capacity.dynamic_limit),
            "active_leases": active, "task_counts": status,
            "requests_used_current_window": capacity.budget_used,
            "max_public_requests_per_minute": MAX_REQUESTS_PER_MINUTE,
            "p95_worker_ms_last_1000": p95,
            "error_tasks": sum(r.error_code is not None for r in rows),
            "execution_allowed": False,
        }

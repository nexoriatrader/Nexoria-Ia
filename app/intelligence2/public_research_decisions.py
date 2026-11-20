"""Non-financial Strategy/Priority/Distribution of SHARED PUBLIC research.

This module publishes immutable *research summaries*, never trading signals.
It reuses the I01-I06 / I04-I05 chain and HistoricalResearchReport, with a
PostgreSQL capacity row shared with the task queue for cross-process idempotency.
No client push, CALL/PUT, account data, order, stake or contract durations.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timezone

from sqlalchemy import select

from .. import db
from ..research_persistence import HistoricalResearchReport
from .public_research_coordinator import (
    VERSION as COORDINATION_VERSION,
    ResearchCapacity, ResearchTask, _pg_session, _row_lock, _clock, _utc,
    claim_public_task, finish_public_task, renew_public_lease,
)
from .research_orchestrator import VERSION as ORCHESTRATOR_VERSION

VERSION = "intelligence20_public_research_decision_pipeline_v1"
_PUBLIC_STATES = frozenset(("RESEARCH_READY", "WAIT_RECHECK", "EXCLUDED"))
_RESTRICTED = frozenset((
    "trade_side", "CALL", "PUT", "entry_epoch", "expiry_minutes",
    "contract_expiry_minutes", "entry_window", "stake", "balance",
    "account_id", "user_id", "pnl", "token", "order_id",
))
_SYMBOL = re.compile(r"[A-Za-z0-9_]{2,30}\Z")


def _public_asset(row, *, now):
    if not isinstance(row, dict) or set(row).intersection(_RESTRICTED):
        return None
    name = row.get("symbol")
    market = row.get("market")
    if (
        not isinstance(name, str) or not _SYMBOL.fullmatch(name)
        or not isinstance(market, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", market)
        or row.get("state") not in _PUBLIC_STATES
        or row.get("execution_allowed") is not False
        or row.get("approved_for_trading") is not False
    ):
        return None
    reasons = row.get("reason_codes")
    if not isinstance(reasons, list):
        return None
    reasons = sorted(set(
        r for r in reasons[:32]
        if isinstance(r, str) and re.fullmatch(r"[A-Z0-9_]{2,80}", r)
    ))
    fingerprint = row.get("fingerprint")
    fingerprint = (fingerprint if isinstance(fingerprint, str)
                   and re.fullmatch(r"[a-f0-9]{64}", fingerprint) else None)
    complete = row.get("evidence_completeness_points")
    if type(complete) is not int or complete not in (0, 20, 40, 60, 80, 100):
        complete = 0
    valid_until = row.get("valid_until_epoch")
    active = type(valid_until) is int and now < valid_until
    if not active or fingerprint is None:
        status = "EXCLUDED"
        reasons = sorted(set(reasons + ["RESEARCH_EXPIRED_OR_UNVERIFIED"]))
    else:
        status = row["state"]
    if status == "RESEARCH_READY" and (
        complete < 100 or row.get("context_consensus") != "THREE_TIMEFRAMES"
        or row.get("evidence_strength") != "CONSISTENT"
    ):
        status = "WAIT_RECHECK"
        reasons = sorted(set(reasons + ["RESEARCH_EVIDENCE_REQUIRES_RECHECK"]))
    return {
        "market": market, "symbol": name,
        "state": status,
        "observed_public_trend": (
            row.get("observed_trend") if row.get("observed_trend") in (
                "UP", "DOWN", "NEUTRAL"
            ) else "NEUTRAL"
        ),
        "evidence_completeness_points": complete,
        "metric_kind": "DATA_COMPLETENESS_NOT_PROBABILITY",
        "fingerprint": fingerprint,
        "valid_until_epoch": valid_until if active else None,
        "reasons": reasons,
        "knowledge_status": (
            row.get("knowledge_base", {}).get("status")
            if isinstance(row.get("knowledge_base"), dict) else "NOT_CONNECTED"
        ),
        "learning_status": (
            row.get("learning_engine", {}).get("status")
            if isinstance(row.get("learning_engine"), dict) else "NOT_CONNECTED"
        ),
        "execution_allowed": False,
    }


def prioritize_public_research(report, *, now=None, max_items=8):
    """Stable public-evidence priority, not ranking of trade profitability."""
    if now is None:
        now = int(time.time())
    if (
        type(now) is not int or now <= 0
        or type(max_items) is not int or not 1 <= max_items <= 8
        or not isinstance(report, dict)
        or report.get("version") != ORCHESTRATOR_VERSION
        or report.get("execution_allowed") is not False
        or report.get("approved_for_trading") is not False
        or report.get("orders_sent") != 0
        or not isinstance(report.get("assets"), list)
        or len(report["assets"]) > 48
        or not isinstance(report.get("distribution"), dict)
        or report["distribution"].get("publish_allowed") is not False
        or _RESTRICTED.intersection(report)
    ):
        raise ValueError("Invalid public research source")
    seen = set()
    rows = []
    for asset in report["assets"]:
        item = _public_asset(asset, now=now)
        if item is None:
            continue
        key = item["market"], item["symbol"]
        if key in seen:
            # Duplicate observation must not be used at all.
            rows = [x for x in rows if (x["market"], x["symbol"]) != key]
            continue
        seen.add(key)
        rows.append(item)
    order = {"RESEARCH_READY": 0, "WAIT_RECHECK": 1, "EXCLUDED": 2}
    rows.sort(key=lambda x: (
        order[x["state"]], -x["evidence_completeness_points"],
        x["market"], x["symbol"],
    ))
    accepted, deferred = rows[:max_items], rows[max_items:]
    return {
        "version": VERSION, "as_of_epoch": now,
        "source_version": report.get("version"),
        "state": "PUBLIC_RESEARCH_ONLY",
        "items": accepted, "deferred_capacity_count": len(deferred),
        "count": len(accepted),
        "quality_measure": "EVIDENCE_COMPLETENESS_NOT_PREDICTION",
        "distribution_kind": "INTERNAL_SHARED_RESEARCH_SNAPSHOT_ONLY",
        "publish_allowed": False,
        "execution_allowed": False,
        "orders_sent": 0,
    }


def persist_public_research_snapshot(snapshot, *, now=None, claim=None, duration_ms=None):
    """Idempotent internal research publication using EXISTING report table.

    Serialized by the same capacity singleton that coordinates worker claims.
    This is an internal persisted record, not a client-facing signal delivery.
    """
    now = _clock(now)
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("version") != VERSION
        or snapshot.get("execution_allowed") is not False
        or snapshot.get("publish_allowed") is not False
        or snapshot.get("orders_sent") != 0
        or snapshot.get("distribution_kind") != "INTERNAL_SHARED_RESEARCH_SNAPSHOT_ONLY"
        or not isinstance(snapshot.get("items"), list)
        or len(snapshot["items"]) > 8
        or _RESTRICTED.intersection(snapshot)
        or any(
            not isinstance(i, dict) or i.get("execution_allowed") is not False
            or _RESTRICTED.intersection(i)
            for i in snapshot["items"]
        )
    ):
        raise ValueError("Untrusted public snapshot")
    body = json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)
    digest = hashlib.sha256(body.encode()).hexdigest()
    key = "I20_PUBLIC_RESEARCH_" + digest[:32]
    with db.SessionLocal() as session:
        _pg_session(session)
        _row_lock(session)
        task = None
        if claim is not None:
            if (
                not isinstance(claim, dict)
                or type(claim.get("id")) is not int
                or type(claim.get("fence")) is not int
                or not isinstance(claim.get("worker_id"), str)
                or type(duration_ms) is not int or duration_ms < 0
            ):
                raise ValueError("Missing fenced public task identity")
            task = session.get(ResearchTask, claim["id"], with_for_update=True)
            if (
                task is None or task.status != "RUNNING"
                or task.worker_id != claim["worker_id"]
                or task.fencing_token != claim["fence"]
                or task.lease_until is None or now >= _utc(task.lease_until)
            ):
                return {"stale": True, "created": False, "digest": None}
        existing = session.scalar(select(HistoricalResearchReport).where(
            HistoricalResearchReport.research_key == key
        ).order_by(HistoricalResearchReport.id.desc()).limit(1))
        if existing is not None:
            if existing.payload_json != body:
                raise ValueError("Public research snapshot key collision")
            identity, created = existing.id, False
        else:
            record = HistoricalResearchReport(
                research_key=key, period_from=now, period_to=now,
                created_at=now, payload_json=body,
            )
            session.add(record)
            session.flush()
            identity, created = record.id, True
        if task is not None:
            # Save public result + finish fenced work in ONE transaction.
            # Lost lease cannot persist, even after another worker recovers it.
            task.status = "DONE"
            task.research_digest = digest
            task.duration_ms = duration_ms
            task.completed_at = now
            task.updated_at = now
            task.worker_id = None
            task.lease_until = None
        session.commit()
        return {"id": identity, "digest": digest, "created": created,
                "stale": False}


async def run_one_public_research_task(*, worker_id, now=None):
    """Opt-in worker entry. Never starts itself and has no broker/order route."""
    # No automatic activation by importing this file; production remains OFF.
    if os.getenv("NEXORA_INTELLIGENCE_DISTRIBUTED_RESEARCH_ENABLED", "false").strip().lower() != "true":
        return {"state": "DISTRIBUTED_PUBLIC_RESEARCH_DISABLED",
                "execution_allowed": False}
    now = _clock(now)
    claim = await asyncio.to_thread(claim_public_task,
                                    worker_id=worker_id, now=now)
    if claim is None:
        return {"state": "NO_PUBLIC_WORK", "execution_allowed": False}
    started = time.monotonic()
    lost_lease = asyncio.Event()

    async def _heartbeat():
        try:
            while True:
                await asyncio.sleep(20)
                alive = await asyncio.to_thread(
                    renew_public_lease, task_id=claim["id"],
                    worker_id=worker_id, fence=claim["fence"],
                    now=datetime.now(timezone.utc),
                )
                if not alive:
                    lost_lease.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            lost_lease.set()

    heartbeat = asyncio.create_task(_heartbeat())
    try:
        # Exactly one existing public research chain; no parallel scanner,
        # duplicate model training or financial execution.
        from .i05_research_adapter import orchestrate_research_with_i04_i05
        source = await orchestrate_research_with_i04_i05(
            now=int(now.timestamp()),
        )
        if lost_lease.is_set():
            return {"state": "STALE_WORKER_FENCED",
                    "id": claim["id"], "execution_allowed": False}
        snapshot = prioritize_public_research(
            source, now=int(datetime.now(timezone.utc).timestamp()),
        )
        elapsed = max(0, round((time.monotonic() - started) * 1000))
        persisted = await asyncio.to_thread(
            persist_public_research_snapshot, snapshot,
            now=datetime.now(timezone.utc), claim=claim,
            duration_ms=elapsed,
        )
        return {
            "state": (
                "STALE_WORKER_FENCED" if persisted.get("stale")
                else "RESEARCH_SAVED"
            ),
            "id": claim["id"], "execution_allowed": False,
        }
    except asyncio.CancelledError:
        # Cancellation leaves only a time-bounded lease; another worker
        # can recover it with a higher fence without accepting stale writes.
        raise
    except Exception:
        elapsed = max(0, round((time.monotonic() - started) * 1000))
        await asyncio.to_thread(
            finish_public_task, task_id=claim["id"], worker_id=worker_id,
            fence=claim["fence"], error_code="PUBLIC_RESEARCH_UNAVAILABLE",
            duration_ms=elapsed, now=datetime.now(timezone.utc),
        )
        return {"state": "RECHECK_PUBLIC_RESEARCH", "execution_allowed": False}
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)

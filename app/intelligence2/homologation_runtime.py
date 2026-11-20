"""Runnable, isolated READ-ONLY homologation facade for NEXORA Intelligence 2.0.

Uses the actual I01-I06 -> I04 -> I05 -> public priority/persistence and
fenced worker chain from the existing research modules. It adds no market
feed, cache, broker/order API, trading signal, or user financial state.

A single shared persisted public snapshot is read for all robot display
contexts; changing display context never changes the intelligence or the
robot's execution controls. OFF in production and other environments.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone

from sqlalchemy import select

from .. import db
from ..research_persistence import HistoricalResearchReport
from .public_research_coordinator import enqueue_public_task
from .public_research_decisions import (
    VERSION as SNAPSHOT_VERSION, run_one_public_research_task,
)

VERSION = "intelligence20_homologation_readonly_v1"
VIEWS = ("autonomous", "signals", "advanced")
MAX_SNAPSHOT_AGE_SECONDS = 120
_SNAPSHOT_PREFIX = "I20_PUBLIC_RESEARCH_"
_ALLOWED_ITEM_KEYS = frozenset((
    "market", "symbol", "state", "observed_public_trend",
    "evidence_completeness_points", "metric_kind", "fingerprint",
    "valid_until_epoch", "reasons", "knowledge_status",
    "learning_status", "execution_allowed",
))
_PUBLIC_STATES = frozenset(("RESEARCH_READY", "WAIT_RECHECK", "EXCLUDED"))


def homologation_enabled() -> bool:
    """Two explicit opt-ins plus a non-production stage are mandatory."""
    return (
        os.getenv("NEXORA_DEPLOYMENT_STAGE", "").strip().lower() == "homologation"
        and os.getenv(
            "NEXORA_INTELLIGENCE_HOMOLOGATION_READONLY_ENABLED", "false"
        ).strip().lower() == "true"
        and os.getenv(
            "NEXORA_INTELLIGENCE_DISTRIBUTED_RESEARCH_ENABLED", "false"
        ).strip().lower() == "true"
        and os.getenv("NEXORA_REAL_AUTOPILOT_ENABLED", "false").strip().lower() != "true"
        and os.getenv("NEXORA_REAL_RELEASE_APPROVED", "false").strip().lower() != "true"
    )


def _safe_public_item(row: object, *, now_epoch: int) -> dict | None:
    """Revalidate untrusted stored JSON; emit only fixed public columns."""
    if not isinstance(row, dict) or set(row) - _ALLOWED_ITEM_KEYS:
        return None
    symbol = row.get("symbol")
    market = row.get("market")
    fingerprint = row.get("fingerprint")
    points = row.get("evidence_completeness_points")
    valid_until = row.get("valid_until_epoch")
    reasons = row.get("reasons")
    if (
        not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9_]{2,30}", symbol)
        or not isinstance(market, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", market)
        or row.get("state") not in _PUBLIC_STATES
        or row.get("execution_allowed") is not False
        or not isinstance(fingerprint, str)
        or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
        or type(points) is not int or points not in (0, 20, 40, 60, 80, 100)
        or type(valid_until) is not int
        or valid_until <= now_epoch
        or not isinstance(reasons, list)
        or len(reasons) > 32
        or not all(isinstance(x, str) and re.fullmatch(r"[A-Z0-9_]{2,80}", x)
                   for x in reasons)
        or row.get("metric_kind") != "DATA_COMPLETENESS_NOT_PROBABILITY"
    ):
        return None
    state = row["state"]
    if state == "RESEARCH_READY" and points != 100:
        state = "WAIT_RECHECK"
    return {
        "market": market,
        "symbol": symbol,
        "review_state": state,
        "evidence_completeness_points": points,
        "metric_kind": "DATA_COMPLETENESS_NOT_PROBABILITY",
        "observation_fingerprint": fingerprint,
        "valid_until_epoch": valid_until,
        "reason_codes": sorted(set(reasons)),
        "knowledge_status": (
            row.get("knowledge_status") if isinstance(
                row.get("knowledge_status"), str
            ) and re.fullmatch(r"[A-Z0-9_]{2,80}", row["knowledge_status"])
            else "UNAVAILABLE"
        ),
        "learning_status": (
            row.get("learning_status") if isinstance(
                row.get("learning_status"), str
            ) and re.fullmatch(r"[A-Z0-9_]{2,80}", row["learning_status"])
            else "UNAVAILABLE"
        ),
    }


def load_shared_public_snapshot(*, now_epoch: int | None = None) -> dict:
    """Read the existing snapshot table, never open a new scanner per user."""
    if now_epoch is None:
        now_epoch = int(time.time())
    if type(now_epoch) is not int or now_epoch <= 0:
        raise ValueError("Invalid homologation observation clock")
    with db.SessionLocal() as session:
        if session.bind.dialect.name != "postgresql":
            raise RuntimeError("Homologation requires existing PostgreSQL persistence")
        # A bounded query into the existing indexed research_key column.
        row = session.scalar(select(HistoricalResearchReport).where(
            HistoricalResearchReport.research_key.like(
                _SNAPSHOT_PREFIX + "%"
            )).order_by(
                HistoricalResearchReport.created_at.desc(),
                HistoricalResearchReport.id.desc(),
            ).limit(1))
        if row is None:
            return {"state": "NO_SHARED_RESEARCH", "items": [],
                    "snapshot_digest": None}
        created = row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        created_epoch = int(created.timestamp())
        if not 0 <= now_epoch - created_epoch < MAX_SNAPSHOT_AGE_SECONDS:
            return {"state": "SNAPSHOT_EXPIRED", "items": [],
                    "snapshot_digest": None}
        try:
            data = json.loads(row.payload_json)
        except (ValueError, TypeError):
            return {"state": "SNAPSHOT_UNTRUSTED", "items": [],
                    "snapshot_digest": None}
        if (
            not isinstance(data, dict)
            or data.get("version") != SNAPSHOT_VERSION
            or data.get("distribution_kind") !=
                "INTERNAL_SHARED_RESEARCH_SNAPSHOT_ONLY"
            or data.get("publish_allowed") is not False
            or data.get("execution_allowed") is not False
            or data.get("orders_sent") != 0
            or not isinstance(data.get("items"), list)
            or len(data["items"]) > 8
            or type(data.get("as_of_epoch")) is not int
            or not 0 <= now_epoch - data["as_of_epoch"] < MAX_SNAPSHOT_AGE_SECONDS
        ):
            return {"state": "SNAPSHOT_UNTRUSTED", "items": [],
                    "snapshot_digest": None}
        output = []
        for candidate in data["items"]:
            item = _safe_public_item(candidate, now_epoch=now_epoch)
            if item is None:
                return {"state": "SNAPSHOT_EXPIRED_OR_UNTRUSTED",
                        "items": [], "snapshot_digest": None}
            output.append(item)
        if len({(x["market"], x["symbol"]) for x in output}) != len(output):
            return {"state": "SNAPSHOT_DUPLICATED", "items": [],
                    "snapshot_digest": None}
        import hashlib
        body = json.dumps(data, sort_keys=True, separators=(",", ":"),
                          allow_nan=False)
        digest = hashlib.sha256(body.encode()).hexdigest()
        if row.research_key != _SNAPSHOT_PREFIX + digest[:32]:
            return {"state": "SNAPSHOT_DIGEST_MISMATCH",
                    "items": [], "snapshot_digest": None}
        return {
            "state": "CURRENT_PUBLIC_RESEARCH",
            "items": output,
            "snapshot_digest": digest,
            "snapshot_created_at_epoch": created_epoch,
        }


def robot_research_view(*, view: str, now_epoch: int | None = None) -> dict:
    """Homologation-only display context, never a robot command or signal."""
    if view not in VIEWS:
        raise ValueError("Unknown robot research display context")
    if not homologation_enabled():
        raise PermissionError("Read-only homologation is not enabled")
    report = load_shared_public_snapshot(now_epoch=now_epoch)
    return {
        "version": VERSION,
        "view": view,
        "state": report["state"],
        "items": report["items"],
        "snapshot_digest": report["snapshot_digest"],
        "shared_intelligence": True,
        "read_only": True,
        "report_kind": "PUBLIC_RESEARCH_CONTEXT_NOT_TRADING_SIGNALS",
        "probability_validated": False,
        "execution_allowed": False,
        "orders_sent": 0,
        "modifies_robot_settings": False,
    }


async def run_homologation_cycle(*, task_key: str, market: str, symbol: str,
                                source: dict, worker_id: str,
                                now: datetime | None = None) -> dict:
    """Explicit manual/CI operation; existing PostgreSQL queue + worker.

    Does not start on import, API GET, app startup, or production runtime.
    Caller supplies only public source identity, not financial instructions.
    """
    if not homologation_enabled():
        raise PermissionError("Homologation execution gate is closed")
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("Timezone-aware clock required")
    queued = await asyncio.to_thread(
        enqueue_public_task, task_key=task_key, market=market,
        symbol=symbol, source=source, now=now,
    )
    outcome = await run_one_public_research_task(
        worker_id=worker_id, now=now,
    )
    return {
        "version": VERSION,
        "enqueue": queued,
        "worker_state": outcome["state"],
        "execution_allowed": False,
        "orders_sent": 0,
    }

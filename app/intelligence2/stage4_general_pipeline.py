"""Isolated synthetic sensor pipeline tracing.

This does not execute financial market-analysis modules or connect a broker.
The only existing research utility reused is I06.research_lifecycle, which
classifies generic evidence revisions and does not produce trade decisions.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import time

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Table, CheckConstraint,
    insert, select, func,
)
from . import stage4_general_homologation as core
from .research_signal_engine import research_lifecycle

REVISION = "nexora_stage4_synthetic_pipeline_trace_v3"
STAGES = ("SYNTHETIC_SOURCE", "VALIDATE", "CLASSIFY", "PERSIST", "I06_EVIDENCE_LIFECYCLE_UTILITY")
trace = Table(
    "nexora_stage4_synthetic_pipeline_trace", core.meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("cycle_key", String(100), nullable=False),
    Column("stage", String(50), nullable=False),
    Column("state", String(16), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column("inputs", Integer, nullable=False),
    Column("outputs", Integer, nullable=False),
    Column("reason", String(90), nullable=False),
    Column("input_digest", String(64), nullable=False),
    Column("output_digest", String(64), nullable=False),
    Column("output_json", Text, nullable=False),
    Column("source", String(40), nullable=False),
    CheckConstraint("source = 'SYNTHETIC_SENSOR_DEMO'",
                    name="ck_stage4_trace_synthetic_only"),
    CheckConstraint("duration_ms >= 0 AND inputs >= 0 AND outputs >= 0",
                    name="ck_stage4_trace_nonnegative"),
)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


class Recorder:
    """In-transaction chain of verified synthetic stages; no network access."""
    def __init__(self, cycle_key: str):
        self.cycle_key = cycle_key
        self.stages: list[dict] = []
        self.started = time.perf_counter()
        self.last_output: object = {"synthetic": True, "cycle_key": cycle_key}

    def step(self, *, stage: str, output: object, inputs: int,
             outputs: int, reason: str = "OK") -> None:
        if stage not in STAGES or inputs < 0 or outputs < 0:
            raise ValueError("Invalid synthetic pipeline stage")
        now = datetime.now(timezone.utc)
        elapsed = max(0, round((time.perf_counter() - self.started) * 1000))
        duration = max(0, elapsed - sum(x["duration_ms"] for x in self.stages))
        payload = {
            "stage": stage, "synthetic": True,
            "outcome": output, "reason": reason, "inputs": inputs,
            "outputs": outputs, "execution_allowed": False,
        }
        row = dict(
            cycle_key=self.cycle_key, stage=stage, state="OK",
            started_at=now, finished_at=now,
            duration_ms=duration, inputs=inputs, outputs=outputs,
            reason=reason, input_digest=_digest(self.last_output),
            output_digest=_digest(payload),
            output_json=json.dumps(payload, ensure_ascii=True, sort_keys=True),
            source=core.SOURCE,
        )
        self.stages.append(row)
        self.last_output = payload

    def commit(self, conn):
        if [s["stage"] for s in self.stages] != list(STAGES):
            raise RuntimeError("Incomplete demonstration pipeline")
        for stage in self.stages:
            conn.execute(insert(trace).values(**stage))


def lifecycle(*, cycle_key: str, duplicate: bool, validated: bool) -> dict:
    """Call ACTUAL I06 lifecycle helper on generic synthetic evidence only.

    This is a single pure I06 utility, NOT I06 evidence engine integration.
    """
    fingerprint = _digest({"cycle_key": cycle_key, "dataset": core.SOURCE})
    result = research_lifecycle(
        previous_record={"fingerprint": fingerprint} if duplicate else None,
        fingerprint=fingerprint, source_current=True, evidence_valid=validated,
    )
    return {
        "utility": "I06.research_lifecycle",
        "scope": "SYNTHETIC_EVIDENCE_REVISION_ONLY",
        "lifecycle": result["lifecycle"],
        "lifecycle_state": result["lifecycle_state"],
        "duplicate_of_previous": result["duplicate_of_previous"],
        "execution_allowed": False,
    }


def recent(limit: int = 50) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid stage trace history limit")
    with core._engine().connect() as conn:
        rows = conn.execute(select(trace).order_by(trace.c.id.desc()).limit(limit)).mappings().all()
    output = []
    for row in rows:
        data = dict(row)
        data["started_at"] = data["started_at"].isoformat()
        data["finished_at"] = data["finished_at"].isoformat()
        data["output"] = json.loads(data.pop("output_json"))
        data["synthetic"] = True
        output.append(data)
    return output


def summary() -> dict:
    with core._engine().connect() as conn:
        total = conn.execute(select(func.count()).select_from(trace)).scalar_one()
        cycles = conn.execute(select(func.count(func.distinct(trace.c.cycle_key)))).scalar_one()
        last = conn.execute(select(trace.c.finished_at).order_by(
            trace.c.id.desc()).limit(1)).scalar_one_or_none()
    return {
        "stage_executions": int(total), "cycles_observed": int(cycles),
        "last_stage_at": last.isoformat() if last else None,
        "stage_names": list(STAGES), "synthetic": True,
        "i06_lifecycle_utility_only": True,
        "full_i06_engine_integrated": False,
    }

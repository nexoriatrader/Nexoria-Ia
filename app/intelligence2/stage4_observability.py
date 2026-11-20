"""Stage 4 isolated general-data observability, NEVER financial operations.

All operational claims are derived from the running synthetic demo and its
PostgreSQL records. I01-I06 exist as source files but are deliberately NOT
integrated or imported by this ASGI service. No broker or trading side effects.
"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import math
import time
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, MetaData, Table,
    CheckConstraint, select, insert, func, desc,
)
from . import stage4_general_homologation as core

REVISION = "nexora_stage4_monitoring_v2"
BOOT_EPOCH = int(time.time())
monitor_runs = Table(
    "nexora_stage4_monitor_runs", core.meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source", String(40), nullable=False),
    Column("cycle_key", String(96), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column("status", String(20), nullable=False),
    Column("processed", Integer, nullable=False),
    Column("inserted", Integer, nullable=False),
    Column("duplicates", Integer, nullable=False),
    Column("incomplete", Integer, nullable=False),
    Column("invalid", Integer, nullable=False),
    Column("reason", String(90), nullable=False),
    CheckConstraint("source = 'SYNTHETIC_SENSOR_DEMO'",
                    name="ck_stage4_run_synthetic"),
    CheckConstraint("processed >= 0 AND inserted >= 0 AND duplicates >= 0 AND "
                    "incomplete >= 0 AND invalid >= 0 AND duration_ms >= 0",
                    name="ck_stage4_run_counts"),
)

MODULES = (
    ("I01", "Market Data", ("market_data",)),
    ("I02", "Market Scanner", ("market_scanner",)),
    ("I03", "Context + Timeframe Engines", ("context_engine", "timeframe_engine")),
    ("I04", "Knowledge Base", ("knowledge_base",)),
    ("I05", "Learning Engine", ("learning_engine",)),
    ("I06", "Research Evidence Engine", ("research_signal_engine",)),
    ("ORCH", "Research Orchestrator", ("research_orchestrator",)),
)
SCOPES = ("GENERAL_SYNTHETIC_DEMO",)
_MAX_SCAN = 150


def record_success(conn, *, cycle_key: str, started: datetime,
                   finished: datetime, duration_ms: int, processed: int,
                   inserted_count: int) -> None:
    """Atomic with the measurement INSERTs; duplicates are NOT rejected data."""
    duplicate_count = processed - inserted_count
    if not (0 <= inserted_count <= processed and duration_ms >= 0):
        raise ValueError("Invalid synthetic pipeline accounting")
    conn.execute(insert(monitor_runs).values(
        source=core.SOURCE, cycle_key=cycle_key,
        started_at=started, finished_at=finished,
        duration_ms=duration_ms, status="OK",
        processed=processed, inserted=inserted_count,
        duplicates=duplicate_count, incomplete=0, invalid=0,
        reason="DUPLICATE_EVENT_KEY" if duplicate_count else "BATCH_ACCEPTED",
    ))


def record_failure(*, error_name: str, duration_ms: int) -> None:
    """Best-effort diagnosis. A PostgreSQL outage is also recorded in Render logs."""
    safe = error_name if error_name.isidentifier() else "UNKNOWN_ERROR"
    when = datetime.now(timezone.utc)
    with core._engine().begin() as conn:
        conn.execute(insert(monitor_runs).values(
            source=core.SOURCE, cycle_key="error_" + str(int(time.time())),
            started_at=when, finished_at=when,
            duration_ms=max(0, int(duration_ms)), status="ERROR",
            processed=0, inserted=0, duplicates=0, incomplete=0,
            invalid=0, reason=safe[:90],
        ))


def _iso(x):
    if x is None:
        return None
    if isinstance(x, datetime):
        return x.isoformat()
    return str(x)


def _run_dict(row):
    data = dict(row)
    data["started_at"] = _iso(data.get("started_at"))
    data["finished_at"] = _iso(data.get("finished_at"))
    data["synthetic"] = True
    return data


def get_runs(limit: int = 35) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid monitoring history limit")
    with core._engine().connect() as conn:
        rows = conn.execute(select(monitor_runs).order_by(
            monitor_runs.c.id.desc()).limit(limit)).mappings().all()
    return [_run_dict(x) for x in rows]


def run_count() -> int:
    with core._engine().connect() as conn:
        return conn.execute(select(func.count()).select_from(monitor_runs)).scalar_one()


def _module_catalog(*, latest_synthetic_at: str | None,
                    demo_fresh: bool, run_count_value: int):
    modules = []
    for name, label, files in MODULES:
        has_code = all(importlib.util.find_spec(
            "app.intelligence2." + file) is not None for file in files)
        modules.append({
            "id": name, "name": label,
            "status": "NOT_CONNECTED" if has_code else "NOT_AVAILABLE",
            "status_label": ("Código disponível · não integrado"
                             if has_code else "Código indisponível"),
            "code_present": has_code,
            "initialized_in_service": False,
            "running_in_service": False,
            "last_update": None,
            "integration": "NO_RUNTIME_TELEMETRY",
            "source": "REPOSITORY_ONLY" if has_code else "NOT_PRESENT",
            "details": ("O módulo existe no repositório, mas não executa neste "
                        "serviço demonstrativo. Sem dados de operação comprovados."
                        if has_code else "Módulo indisponível no código instalado."),
        })
    modules.extend((
        {"id": "DEMO", "name": "Ingestão e classificação sintética",
         "status": "AVAILABLE" if demo_fresh and run_count_value else "DEGRADED",
         "status_label": ("Ativo · dados sintéticos" if demo_fresh and run_count_value
                          else "Sem atualização recente"),
         "code_present": True, "initialized_in_service": True,
         "running_in_service": bool(demo_fresh and run_count_value),
         "last_update": latest_synthetic_at,
         "integration": "POSTGRES_DEMO_PERSISTENCE",
         "source": core.SOURCE,
         "details": "Somente sensores fictícios, sem conexão com corretoras."},
        {"id": "PG", "name": "PostgreSQL · histórico",
         "status": "AVAILABLE", "status_label": "Conectado · consulta confirmada",
         "code_present": True, "initialized_in_service": True,
         "running_in_service": True, "last_update": latest_synthetic_at,
         "integration": "DEDICATED_HOMOLOGATION_DATABASE",
         "source": "POSTGRESQL",
         "details": "Leituras e execuções armazenadas no banco exclusivo."},
    ))
    return modules


def get_overview(*, runs_limit: int = 20, records_limit: int = 20):
    """Read one consistent database snapshot: stored facts, not fake health."""
    if not 1 <= runs_limit <= 100 or not 1 <= records_limit <= 100:
        raise ValueError("Invalid dashboard pagination")
    with core._engine().connect() as conn:
        total = conn.execute(select(func.count()).select_from(core.measurements)).scalar_one()
        last = conn.execute(select(core.measurements.c.observed_at).order_by(
            core.measurements.c.id.desc()).limit(1)).scalar_one_or_none()
        last_written = conn.execute(select(core.measurements.c.recorded_at).order_by(
            core.measurements.c.id.desc()).limit(1)).scalar_one_or_none()
        counts = conn.execute(select(core.measurements.c.classification,
                                     func.count()).group_by(
            core.measurements.c.classification)).all()
        aggregates = conn.execute(select(
            func.count(monitor_runs.c.id),
            func.coalesce(func.sum(monitor_runs.c.processed), 0),
            func.coalesce(func.sum(monitor_runs.c.inserted), 0),
            func.coalesce(func.sum(monitor_runs.c.duplicates), 0),
            func.coalesce(func.sum(monitor_runs.c.incomplete), 0),
            func.coalesce(func.sum(monitor_runs.c.invalid), 0),
            func.coalesce(func.sum(func.cast(monitor_runs.c.status == "ERROR", Integer)), 0),
            func.avg(monitor_runs.c.duration_ms),
        )).one()
        recent = conn.execute(select(monitor_runs).order_by(
            monitor_runs.c.id.desc()).limit(runs_limit)).mappings().all()
        records = conn.execute(select(core.measurements).order_by(
            core.measurements.c.id.desc()).limit(records_limit)).mappings().all()
        sample = conn.execute(select(
            core.measurements.c.source, core.measurements.c.metric,
            core.measurements.c.value, core.measurements.c.unit,
            core.measurements.c.classification,
            core.measurements.c.event_key,
        ).order_by(core.measurements.c.id.desc()).limit(_MAX_SCAN)).all()
        revisions = set(conn.execute(select(core.revisions.c.revision)).scalars())

    # Measure actual row integrity over a declared sample. Never claim full scan.
    bad = 0
    for src, metric, value, unit, classification, key in sample:
        if (src != core.SOURCE or metric not in core.VALUES
                or not isinstance(value, (int, float)) or not math.isfinite(value)
                or not unit or not key or classification != core._classify(metric, value)):
            bad += 1
    last_at = _iso(last)
    elapsed = (datetime.now(timezone.utc).timestamp() - last.timestamp()
               if last is not None else None)
    interval = core._interval()
    fresh = bool(elapsed is not None and 0 <= elapsed <= interval * 3)
    all_runs = [_run_dict(r) for r in recent]
    severity = []
    for r in all_runs:
        if r["status"] == "ERROR":
            severity.append({"level": "ERROR", "at": r["finished_at"],
                             "code": r["reason"], "details": "Falha da execução demonstrativa"})
        elif r["duplicates"]:
            severity.append({"level": "INFO", "at": r["finished_at"],
                             "code": "DUPLICATE_EVENT_KEY",
                             "details": str(r["duplicates"]) +
                                        " leitura(s) idempotente(s) já persistida(s)"})
    if bad:
        severity.insert(0, {"level": "ERROR", "at": _iso(datetime.now(timezone.utc)),
                            "code": "INTEGRITY_SAMPLE_MISMATCH",
                            "details": str(bad) + " registro(s) divergente(s) na amostra"})
    warnings_count = sum(r["duplicates"] > 0 for r in all_runs)
    return {
        "version": "nexora_stage4_monitoring_v2",
        "environment": "homologation",
        "data_source": core.SOURCE,
        "synthetic": True, "financial_execution": False,
        "external_feed_connected": False,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "health": {
            "database": "AVAILABLE",
            "dataset_freshness": ("CURRENT" if fresh else "STALE"),
            "last_ingestion_age_seconds": round(elapsed, 1) if elapsed is not None else None,
            "uptime_seconds": max(0, int(time.time()) - BOOT_EPOCH),
            "poll_interval_seconds": interval,
            "integrity_sample": {"checked_rows": len(sample),
                                 "mismatches": bad,
                                 "status": "PASS" if bad == 0 else "FAILED"},
            "schema_revision_installed": REVISION in revisions,
        },
        "summary": {
            "stored_records": int(total),
            "total_runs": int(aggregates[0]),
            "processed_observations": int(aggregates[1]),
            "new_insertions_observed_since_monitoring": int(aggregates[2]),
            "duplicate_observations": int(aggregates[3]),
            "incomplete_observations": int(aggregates[4]),
            "invalid_observations": int(aggregates[5]),
            "failed_runs": int(aggregates[6]),
            "mean_processing_ms": round(float(aggregates[7]), 1) if aggregates[7] is not None else None,
            "classification_totals": {str(k): int(v) for k, v in counts},
            "latest_observed_at": last_at,
            "latest_recorded_at": _iso(last_written),
            "module_connected_count": 0,
            "module_total": len(MODULES),
            "diagnostics_in_recent_runs": warnings_count,
        },
        "modules": _module_catalog(
            latest_synthetic_at=last_at, demo_fresh=fresh,
            run_count_value=int(aggregates[0])),
        "runs": all_runs,
        "records": [core._serialize(r) for r in records],
        "diagnostics": severity[:20],
        "limitations": (
            "Dados de sensores são sintéticos; I01–I06 e orquestrador não são "
            "executados neste serviço isolado.",
            "Histórico de execuções iniciado na revisão de monitoramento v2; "
            "leituras anteriores permanecem na tabela original.",
            "Integridade verificada em amostra recente de até " + str(_MAX_SCAN) +
            " registros; não representa auditoria integral da base.",
        ),
    }

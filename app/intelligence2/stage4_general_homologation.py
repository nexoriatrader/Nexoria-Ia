"""Standalone Stage 4 general synthetic-data homologation application.

Never imports app.main, Deriv, gambling, broker, or financial execution paths.
The ONLY data source is a clearly labeled deterministic synthetic sensor feed.
PostgreSQL is exclusive to the approved isolated homologation database.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import math
import hmac
import os
import time
import logging

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pathlib import Path
from sqlalchemy import (
    CheckConstraint, Column, DateTime, Float, Integer, MetaData, String,
    Table, UniqueConstraint, create_engine, func, insert, select, text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import make_url

log = logging.getLogger("nexora.stage4.general_demo")
SCHEMA_REVISION = "nexora_stage4_general_synthetic_v1"
EXPECTED_DATABASE = "nexora_intelligence20_homologation_pg"
SOURCE = "SYNTHETIC_SENSOR_DEMO"
meta = MetaData()
measurements = Table(
    "nexora_stage4_demo_measurements", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_key", String(96), nullable=False, unique=True),
    Column("station", String(24), nullable=False),
    Column("metric", String(24), nullable=False),
    Column("value", Float, nullable=False),
    Column("unit", String(12), nullable=False),
    Column("classification", String(24), nullable=False),
    Column("source", String(40), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("source = 'SYNTHETIC_SENSOR_DEMO'",
                    name="ck_stage4_demo_synthetic_source"),
    CheckConstraint("classification IN ('NORMAL', 'REVIEW', 'OUTSIDE_RANGE')",
                    name="ck_stage4_demo_classification"),
)
revisions = Table(
    "nexora_stage4_demo_schema_revisions", meta,
    Column("revision", String(80), primary_key=True),
    Column("applied_at", DateTime(timezone=True), nullable=False),
)
STATIONS = (("LAB-A", "temperature", "C"),
            ("LAB-B", "humidity", "pct"),
            ("LAB-C", "illumination", "lux"))
VALUES = {
    "temperature": (16.0, 32.0),
    "humidity": (25.0, 78.0),
    "illumination": (100.0, 1200.0),
}
_last_cycle = {"state": "PENDING", "source": SOURCE}


def require_isolated_postgres() -> str:
    """Fail before ANY DB access outside the designated test workspace DB."""
    if os.getenv("NEXORA_STAGE4_GENERAL_SYNTHETIC_ONLY") != "true":
        raise RuntimeError("Synthetic-only homologation mode is required")
    raw = os.getenv("DATABASE_URL", "")
    if raw.startswith("postgres://"):
        raw = "postgresql+psycopg://" + raw[len("postgres://"):]
    elif raw.startswith("postgresql://"):
        raw = "postgresql+psycopg://" + raw[len("postgresql://"):]
    if not raw.startswith("postgresql+psycopg://"):
        raise RuntimeError("Isolated PostgreSQL connection required")
    parsed = make_url(raw)
    if (parsed.database != EXPECTED_DATABASE or not parsed.host
            or not parsed.username):
        raise RuntimeError("Refusing DB: not the dedicated Stage 4 homologation database")
    return raw


@lru_cache(maxsize=1)
def _engine():
    return create_engine(require_isolated_postgres(), pool_pre_ping=True,
                         pool_size=3, max_overflow=2, pool_recycle=180)


def _interval() -> int:
    raw = os.getenv("NEXORA_STAGE4_INGEST_INTERVAL_SECONDS", "90")
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise RuntimeError("Invalid synthetic ingest interval") from exc
    if not 30 <= seconds <= 300:
        raise RuntimeError("Synthetic ingest interval must be 30-300 seconds")
    return seconds


def bootstrap_schema() -> None:
    """Operator-authorized initial migration, invoked by start command only.

    SQL is never executed on production: assert DB identity and isolated flag.
    Safe to run on every isolated service restart; additive/idempotent.
    """
    require_isolated_postgres()
    engine = _engine()
    meta.create_all(engine, checkfirst=True)
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        found = conn.execute(select(revisions.c.revision).where(
            revisions.c.revision == SCHEMA_REVISION)).scalar_one_or_none()
        if found is None:
            conn.execute(insert(revisions).values(
                revision=SCHEMA_REVISION, applied_at=now))
        monitor_found = conn.execute(select(revisions.c.revision).where(
            revisions.c.revision == observer.REVISION)).scalar_one_or_none()
        if monitor_found is None:
            conn.execute(insert(revisions).values(
                revision=observer.REVISION, applied_at=now))
        trace_found = conn.execute(select(revisions.c.revision).where(
            revisions.c.revision == pipeline.REVISION)).scalar_one_or_none()
        if trace_found is None:
            conn.execute(insert(revisions).values(
                revision=pipeline.REVISION, applied_at=now))
        ohlc_found = conn.execute(select(revisions.c.revision).where(
            revisions.c.revision == ohlc.REVISION)).scalar_one_or_none()
        if ohlc_found is None:
            conn.execute(insert(revisions).values(
                revision=ohlc.REVISION, applied_at=now))
    log.warning("stage4_general_schema_verified revision=%s", SCHEMA_REVISION)


def verify_schema() -> None:
    """Read-only application startup. No automatic DDL."""
    require_isolated_postgres()
    with _engine().connect() as conn:
        found = conn.execute(select(revisions.c.revision).where(
            revisions.c.revision == SCHEMA_REVISION)).scalar_one_or_none()
        if found != SCHEMA_REVISION:
            raise RuntimeError("Stage4 demo migration not installed")
        active_monitor = conn.execute(select(revisions.c.revision).where(
            revisions.c.revision == observer.REVISION)).scalar_one_or_none()
        if active_monitor != observer.REVISION:
            raise RuntimeError("Stage4 monitoring migration not installed")
        active_trace = conn.execute(select(revisions.c.revision).where(
            revisions.c.revision == pipeline.REVISION)).scalar_one_or_none()
        if active_trace != pipeline.REVISION:
            raise RuntimeError("Stage4 synthetic pipeline trace migration not installed")
        active_ohlc = conn.execute(select(revisions.c.revision).where(
            revisions.c.revision == ohlc.REVISION)).scalar_one_or_none()
        if active_ohlc != ohlc.REVISION:
            raise RuntimeError("Offline OHLC quality schema not installed")


def _value(bucket: int, station: str, metric: str) -> float:
    digest = hashlib.sha256(
        f"stage4-synthetic-v1:{bucket}:{station}:{metric}".encode()
    ).digest()
    ratio = int.from_bytes(digest[:8], "big") / (2**64 - 1)
    minimum, maximum = VALUES[metric]
    return round(minimum + (maximum - minimum) * ratio, 2)


def _classify(metric: str, value: float) -> str:
    stable = {
        "temperature": (19.0, 27.0),
        "humidity": (35.0, 65.0),
        "illumination": (300.0, 900.0),
    }
    low, high = stable[metric]
    if low <= value <= high:
        return "NORMAL"
    if (low - (high - low) * .20) <= value <= (high + (high - low) * .20):
        return "REVIEW"
    return "OUTSIDE_RANGE"


def ingest_batch(*, now_epoch: int | None = None,
                 interval: int | None = None) -> dict:
    """Process and persist one idempotent batch of synthetic sensor readings."""
    interval = _interval() if interval is None else interval
    if type(interval) is not int or not 30 <= interval <= 300:
        raise ValueError("Invalid ingest interval")
    now_epoch = int(time.time()) if now_epoch is None else now_epoch
    if type(now_epoch) is not int or now_epoch <= 0:
        raise ValueError("Invalid observation timestamp")
    bucket = now_epoch // interval
    observed = datetime.fromtimestamp(bucket * interval, tz=timezone.utc)
    written = datetime.now(timezone.utc)
    new_rows = 0
    started_perf = time.perf_counter()
    recorder = pipeline.Recorder(cycle_key="SYNTHETIC_" + str(bucket))
    observed_inputs = [
        {"station": station, "metric": metric, "unit": unit,
         "value": _value(bucket, station, metric)}
        for station, metric, unit in STATIONS
    ]
    recorder.step(stage="SYNTHETIC_SOURCE",
                  output={"stations": len(observed_inputs), "source": SOURCE,
                          "observed_at": observed.isoformat()},
                  inputs=0, outputs=len(observed_inputs),
                  reason="DETERMINISTIC_SYNTHETIC_READINGS")
    valid = all(
        isinstance(row["value"], float) and math.isfinite(row["value"])
        and bool(row["station"]) and row["metric"] in VALUES and bool(row["unit"])
        for row in observed_inputs
    )
    recorder.step(stage="VALIDATE",
                  output={"validated": valid, "count": len(observed_inputs)},
                  inputs=len(observed_inputs),
                  outputs=len(observed_inputs) if valid else 0,
                  reason="FINITE_COMPLETE_SENSOR_VALUES" if valid else "INVALID_INPUT")
    if not valid:
        raise ValueError("Invalid synthetic sensor measurements")
    classified_inputs = [
        {**row, "classification": _classify(row["metric"], row["value"])}
        for row in observed_inputs
    ]
    recorder.step(stage="CLASSIFY",
                  output={"classifications": {
                    state: sum(item["classification"] == state for item in classified_inputs)
                    for state in ("NORMAL", "REVIEW", "OUTSIDE_RANGE")
                  }},
                  inputs=len(observed_inputs), outputs=len(classified_inputs),
                  reason="DETERMINISTIC_SENSOR_CLASSIFICATION")
    with _engine().begin() as conn:
        for item in classified_inputs:
            station, metric, unit = item["station"], item["metric"], item["unit"]
            value = item["value"]
            row = dict(
                event_key=f"SYNTH_{bucket}_{station}_{metric}",
                station=station, metric=metric, value=value, unit=unit,
                classification=item["classification"],
                source=SOURCE, observed_at=observed, recorded_at=written,
            )
            if conn.dialect.name == "postgresql":
                stmt = pg_insert(measurements).values(**row).on_conflict_do_nothing(
                    index_elements=["event_key"])
            elif conn.dialect.name == "sqlite":
                stmt = sqlite_insert(measurements).values(**row).on_conflict_do_nothing(
                    index_elements=["event_key"])
            else:
                raise RuntimeError("Unsupported demo database engine")
            # psycopg may report rowcount=-1 for INSERT .. ON CONFLICT.
            # RETURNING distinguishes an actual insert from an idempotent skip.
            inserted_id = conn.execute(
                stmt.returning(measurements.c.id)
            ).scalar_one_or_none()
            if inserted_id is not None:
                new_rows += 1
        recorder.step(stage="PERSIST",
                      output={"inserted": new_rows,
                              "duplicates": len(STATIONS) - new_rows,
                              "storage": "POSTGRESQL_HOMOLOGATION"},
                      inputs=len(STATIONS), outputs=new_rows,
                      reason="ON_CONFLICT_DO_NOTHING")
        utility = pipeline.lifecycle(
            cycle_key=recorder.cycle_key, duplicate=(new_rows == 0),
            validated=True,
        )
        recorder.step(stage="I06_EVIDENCE_LIFECYCLE_UTILITY",
                      output=utility, inputs=1, outputs=1,
                      reason="UTILITY_ONLY_NOT_FULL_I06_ENGINE")
        recorder.commit(conn)
        observer.record_success(
            conn, cycle_key="SYNTHETIC_" + str(bucket),
            started=written, finished=datetime.now(timezone.utc),
            duration_ms=max(0, round((time.perf_counter()-started_perf)*1000)),
            processed=len(STATIONS), inserted_count=new_rows,
        )
    return {
        "state": "SYNTHETIC_BATCH_PERSISTED", "source": SOURCE,
        "observed_at": observed.isoformat(), "inserted": new_rows,
        "batch_size": len(STATIONS), "simulation": True,
    }


def _serialize(row) -> dict:
    d = dict(row)
    for key in ("observed_at", "recorded_at"):
        d[key] = d[key].isoformat() if d.get(key) else None
    return d


def read_history(limit: int = 25) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid history limit")
    with _engine().connect() as conn:
        rows = conn.execute(select(measurements).order_by(
            measurements.c.id.desc()).limit(limit)).mappings().all()
    return [_serialize(row) for row in rows]


def read_status() -> dict:
    with _engine().connect() as conn:
        count = conn.execute(select(func.count()).select_from(measurements)).scalar_one()
        last = conn.execute(select(measurements.c.observed_at).order_by(
            measurements.c.id.desc()).limit(1)).scalar_one_or_none()
        kinds = conn.execute(select(measurements.c.classification,
                                    func.count()).group_by(
            measurements.c.classification)).all()
        revision = conn.execute(select(revisions.c.revision).where(
            revisions.c.revision == SCHEMA_REVISION)).scalar_one_or_none()
    return {
        "application": "NEXORA 2.0 — isolated general-data demo",
        "state": "DATABASE_READY" if revision == SCHEMA_REVISION else "MIGRATION_REQUIRED",
        "dataset": SOURCE,
        "synthetic": True,
        "records": count,
        "latest_observed_at": last.isoformat() if last else None,
        "classifications": {key: value for key, value in kinds},
        "last_cycle": dict(_last_cycle),
        "financial_execution": False,
        "external_feed_connected": False,
    }


async def _loop(stop: asyncio.Event):
    global _last_cycle
    interval = _interval()
    while not stop.is_set():
        try:
            _last_cycle = await asyncio.to_thread(ingest_batch, interval=interval)
            log.info("stage4_general_batch source=%s inserted=%s observed=%s",
                     SOURCE, _last_cycle["inserted"], _last_cycle["observed_at"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _last_cycle = {"state": "INGEST_UNAVAILABLE", "source": SOURCE}
            log.exception("stage4_general_ingest_error")
            try:
                await asyncio.to_thread(
                    observer.record_failure,
                    error_name=type(exc).__name__, duration_ms=0,
                )
            except Exception:
                log.warning("stage4_general_error_audit_unavailable")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Verify DB persistence and real HTTP routes on every isolated restart."""
    verify_schema()
    old_archive = await asyncio.to_thread(ohlc.read_audits, limit=1)
    quality = await asyncio.to_thread(ohlc.record_fixture_audit)
    after_archive = await asyncio.to_thread(ohlc.read_audits, limit=1)
    log.warning(
        "stage4_offline_ohlc_archive prior_audits=%s after_audits=%s "
        "new_entry=%s quality=%s bars=%s synthetic=true",
        old_archive["total"], after_archive["total"], quality["inserted"],
        quality["quality"], quality["bar_count"],
    )
    old_runs = await asyncio.to_thread(observer.run_count)
    old_traces = await asyncio.to_thread(pipeline.summary)
    log.warning("stage4_monitor_restart existing_persisted_runs=%s", old_runs)
    log.warning("stage4_pipeline_restart prior_stage_rows=%s prior_cycles=%s",
                old_traces["stage_executions"], old_traces["cycles_observed"])
    before = await asyncio.to_thread(read_status)
    log.warning("stage4_restart_check records_before_first_batch=%s previous_observation=%s",
                before["records"], before["latest_observed_at"])
    first = await asyncio.to_thread(ingest_batch, interval=_interval())
    after = await asyncio.to_thread(read_status)
    log.warning("stage4_live_ingestion dataset=%s inserted=%s total_records=%s",
                SOURCE, first["inserted"], after["records"])

    # The HTTP self-test invokes the actual ASGI routes with the Render-generated
    # test secret, never outputs or retains the secret, and never touches a broker.
    import httpx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://stage4-internal-selftest",
        timeout=12,
    ) as client:
        unauthorized = await client.get("/api/homologation/demo/status")
        headers = {"X-NEXORA-STAGE4-KEY": os.environ["NEXORA_STAGE4_ACCESS_TOKEN"]}
        authorized = await client.get("/api/homologation/demo/status",
                                      headers=headers)
        history = await client.get("/api/homologation/demo/history?limit=25",
                                   headers=headers)
        panel = await client.get("/")
        health = await client.get("/healthz")
        monitor_missing = await client.get("/api/homologation/monitor/overview")
        monitor = await client.get("/api/homologation/monitor/overview", headers=headers)
        runlog = await client.get("/api/homologation/monitor/runs", headers=headers)
        stages = await client.get("/api/homologation/monitor/pipeline?limit=20", headers=headers)
        missing_stage_key = await client.get("/api/homologation/monitor/pipeline")
        missing_ohlc_key = await client.get("/api/homologation/monitor/ohlc-quality")
        archived_ohlc = await client.get("/api/homologation/monitor/ohlc-quality",
                                         headers=headers)
    if not (
        unauthorized.status_code == 401
        and authorized.status_code == 200
        and history.status_code == 200
        and panel.status_code == 200
        and health.status_code == 200
        and monitor_missing.status_code == 401
        and monitor.status_code == 200
        and runlog.status_code == 200
        and stages.status_code == 200
        and missing_stage_key.status_code == 401
        and missing_ohlc_key.status_code == 401
        and archived_ohlc.status_code == 200
        and archived_ohlc.json().get("total",0) >= 1
        and archived_ohlc.json().get("i01_module_integrated") is False
        and stages.json().get("summary",{}).get("stage_executions",0) >= len(pipeline.STAGES)
        and monitor.json().get("summary",{}).get("stored_records",0) >= len(STATIONS)
        and monitor.json().get("summary",{}).get("total_runs",0) >= 1
        and authorized.json().get("records", 0) >= len(STATIONS)
        and history.json().get("count", 0) >= len(STATIONS)
        and health.json().get("database") == "DATABASE_READY"
    ):
        raise RuntimeError("Stage4 authenticated HTTP and PostgreSQL smoke failed")
    log.warning(
        "stage4_http_pg_smoke auth_missing=401 auth_valid=200 history=200 "
        "panel=200 health=200 total_records=%s dataset=%s",
        authorized.json()["records"], SOURCE,
    )
    log.warning(
        "stage4_pipeline_smoke auth_missing=401 trace=200 stages=%s cycles=%s",
        stages.json()["summary"]["stage_executions"],
        stages.json()["summary"]["cycles_observed"],
    )
    log.warning(
        "stage4_ohlc_smoke missing_key=401 valid_key=200 "
        "archived_snapshots=%s source=%s i01_integrated=false",
        archived_ohlc.json()["total"], ohlc.FIXTURE,
    )
    log.warning(
        "stage4_monitor_smoke auth_missing=401 overview=200 runs=200 "
        "modules=%s persisted_runs=%s records=%s",
        len(monitor.json().get("modules",[])),
        monitor.json()["summary"]["total_runs"],
        monitor.json()["summary"]["stored_records"],
    )

    stop = asyncio.Event()
    task = asyncio.create_task(_loop(stop), name="stage4-synthetic-sensor-ingest")
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title="NEXORA 2.0 — Homologação técnica independente",
              lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)


@app.middleware("http")
async def headers(request, call_next):
    response = await call_next(request)
    response.headers.update({
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "form-action 'none'; frame-ancestors 'none'"
        ),
    })
    return response


def _auth(token: str | None):
    secret = os.getenv("NEXORA_STAGE4_ACCESS_TOKEN", "")
    if len(secret) < 32:
        raise HTTPException(503, "Demo access not configured")
    if token is None or not hmac.compare_digest(secret, token):
        raise HTTPException(401, "Access key required")


@app.get("/healthz")
async def healthz():
    try:
        state = await asyncio.to_thread(read_status)
        return {"status": "ok", "dataset": SOURCE, "records": state["records"],
                "synthetic": True, "database": state["state"],
                "external_feed_connected": False}
    except Exception:
        log.exception("stage4_demo_health_db_unavailable")
        return JSONResponse({"status": "unavailable", "database": "ERROR",
                             "synthetic": True}, status_code=503)


@app.get("/api/homologation/demo/status")
async def status(x_nexora_stage4_key: str | None = Header(default=None)):
    _auth(x_nexora_stage4_key)
    try:
        return await asyncio.to_thread(read_status)
    except Exception:
        log.exception("stage4_demo_status_error")
        raise HTTPException(503, "Demo database unavailable") from None


@app.get("/api/homologation/demo/history")
async def history(limit: int = Query(default=25, ge=1, le=100),
                  x_nexora_stage4_key: str | None = Header(default=None)):
    _auth(x_nexora_stage4_key)
    try:
        rows = await asyncio.to_thread(read_history, limit)
        return {"source": SOURCE, "synthetic": True, "count": len(rows), "rows": rows}
    except Exception:
        log.exception("stage4_demo_history_error")
        raise HTTPException(503, "Demo history unavailable") from None


_HTML = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NEXORA Intelligence 2.0 | Homologação</title>
<style>
body{font-family:system-ui,sans-serif;background:#091827;color:#e9f6fa;
max-width:1080px;margin:0 auto;padding:26px}
h1{font-size:1.8rem;margin-bottom:4px}p,small{color:#bed0db}
section{background:#152c42;padding:20px;margin:16px 0;border-radius:12px}
input,button{font:inherit;padding:10px;margin:6px;border-radius:6px}
button{background:#10bfa8;color:#05242a;border:0;cursor:pointer}
input{max-width:90%;width:340px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;color:#ccebea}
strong{color:#42e9d0}table{border-collapse:collapse;width:100%}
td,th{padding:8px;border-bottom:1px solid #355064;text-align:left}
</style></head><body>
<h1>NEXORA Intelligence 2.0</h1>
<p><strong>HOMOLOGAÇÃO ISOLADA · DADOS SINTÉTICOS</strong></p>
<p>Demonstração técnica de sensores, ingestão periódica, classificação e
persistência PostgreSQL. Sem dados de corretoras, apostas ou execução financeira.</p>
<section><label>Chave de acesso <input id="key" type="password"
autocomplete="off" placeholder="Chave exclusiva do Render"></label>
<button id="refresh" type="button">Consultar dados</button>
<p><small>A chave fica apenas na memória desta página e não é salva.</small></p></section>
<section><h2>Estado</h2><pre id="status">Informe a chave e clique em Consultar.</pre></section>
<section><h2>Histórico persistido</h2><table><thead><tr>
<th>Observado (UTC)</th><th>Estação</th><th>Métrica</th>
<th>Valor</th><th>Classificação</th></tr></thead>
<tbody id="rows"></tbody></table></section>
<script>
async function api(path){
const token=document.getElementById('key').value;
const response=await fetch(path,{headers:{'X-NEXORA-STAGE4-KEY':token},
cache:'no-store'});
if(!response.ok)throw Error('HTTP '+response.status);
return response.json();
}
async function refresh(){
const status=document.getElementById('status'),rows=document.getElementById('rows');
try{
const [s,h]=await Promise.all([api('/api/homologation/demo/status'),
api('/api/homologation/demo/history?limit=25')]);
status.textContent=JSON.stringify(s,null,2);
rows.textContent='';
for(const v of h.rows){
const tr=document.createElement('tr');
for(const field of ['observed_at','station','metric','value','classification']){
const td=document.createElement('td');td.textContent=String(v[field]);tr.appendChild(td);
}rows.appendChild(tr);
}
}catch(e){status.textContent='Consulta indisponível: '+e.message;rows.textContent='';}
}
document.getElementById('refresh').addEventListener('click',refresh);
</script></body></html>"""


@app.get("/api/homologation/monitor/overview")
async def monitoring_overview(
    runs_limit: int = Query(default=20, ge=1, le=100),
    records_limit: int = Query(default=20, ge=1, le=100),
    x_nexora_stage4_key: str | None = Header(default=None),
):
    _auth(x_nexora_stage4_key)
    try:
        return await asyncio.to_thread(
            observer.get_overview, runs_limit=runs_limit,
            records_limit=records_limit,
        )
    except Exception:
        log.exception("stage4_monitor_overview_unavailable")
        raise HTTPException(503, "Monitoring data unavailable") from None


@app.get("/api/homologation/monitor/runs")
async def monitoring_runs(
    limit: int = Query(default=35, ge=1, le=100),
    x_nexora_stage4_key: str | None = Header(default=None),
):
    _auth(x_nexora_stage4_key)
    try:
        history = await asyncio.to_thread(observer.get_runs, limit=limit)
        return {"source": SOURCE, "synthetic": True,
                "count": len(history), "runs": history}
    except Exception:
        log.exception("stage4_monitor_runs_unavailable")
        raise HTTPException(503, "Processing history unavailable") from None


@app.get("/api/homologation/monitor/pipeline")
async def pipeline_trace(
    limit: int = Query(default=50, ge=1, le=100),
    x_nexora_stage4_key: str | None = Header(default=None),
):
    _auth(x_nexora_stage4_key)
    try:
        recent = await asyncio.to_thread(pipeline.recent, limit=limit)
        summary = await asyncio.to_thread(pipeline.summary)
        return {"source": SOURCE, "synthetic": True,
                "stages": recent, "summary": summary,
                "full_I01_to_I06_integrated": False}
    except Exception:
        log.exception("stage4_pipeline_trace_unavailable")
        raise HTTPException(503, "Synthetic pipeline trace unavailable") from None


@app.get("/api/homologation/monitor/ohlc-quality")
async def offline_ohlc_quality(
    limit: int = Query(default=10, ge=1, le=30),
    x_nexora_stage4_key: str | None = Header(default=None),
):
    _auth(x_nexora_stage4_key)
    try:
        return await asyncio.to_thread(ohlc.read_audits, limit)
    except Exception:
        log.exception("stage4_offline_ohlc_query_unavailable")
        raise HTTPException(503, "Historical OHLC quality audit unavailable") from None


@app.get("/api/homologation/monitor/modules")
async def monitoring_modules(x_nexora_stage4_key: str | None = Header(default=None)):
    _auth(x_nexora_stage4_key)
    try:
        report = await asyncio.to_thread(observer.get_overview, runs_limit=1, records_limit=1)
        return {"modules": report["modules"], "synthetic": True,
                "generated_at": report["generated_at"]}
    except Exception:
        log.exception("stage4_monitor_modules_unavailable")
        raise HTTPException(503, "Module status unavailable") from None


@app.get("/api/homologation/monitor/diagnostics")
async def monitoring_diagnostics(x_nexora_stage4_key: str | None = Header(default=None)):
    _auth(x_nexora_stage4_key)
    try:
        report = await asyncio.to_thread(observer.get_overview, runs_limit=70, records_limit=1)
        return {"diagnostics": report["diagnostics"],
                "health": report["health"], "limitations": report["limitations"],
                "synthetic": True, "generated_at": report["generated_at"]}
    except Exception:
        log.exception("stage4_monitor_diagnostics_unavailable")
        raise HTTPException(503, "Diagnostics unavailable") from None


@app.get("/")
async def index():
    return FileResponse(Path(__file__).with_name("stage4_monitor_dashboard.html"),
                        media_type="text/html")


# Import only isolated general observability metadata after the core tables
# exist; this registers the additive monitoring table for offline bootstrap.
from . import stage4_observability as observer
from . import stage4_general_pipeline as pipeline
from . import stage4_ohlc_quality as ohlc


"""Real PostgreSQL 16 public research capacity, migration, and worker fencing.

NO user accounts, trades, DEMO/REAL operations, Deriv API, financial results,
or production DB. Applied F01/I04/coordination SQL is strictly ephemeral.
"""
from __future__ import annotations

import multiprocessing as mp
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading

import pytest
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app import db
from app.research_persistence import HistoricalResearchReport
from app.intelligence2 import public_research_coordinator as coord
from app.intelligence2 import public_research_decisions as decisions
from app.intelligence2.public_research_coordinator import (
    ResearchCapacity, ResearchTask, CoordinationMetadata,
)

PG = os.getenv("NEXORA_CONSOLIDATION_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not PG.startswith("postgresql+psycopg://"),
    reason="PostgreSQL disposable database required; not a mock",
)
ROOT = Path(__file__).resolve().parents[1] / "migrations"
CLOCK = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)


def _migration(engine, name):
    sql = (ROOT / name).read_text()
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(sql, prepare=False)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


@pytest.fixture(scope="module")
def pg():
    engine = create_engine(PG, pool_pre_ping=True, pool_size=8, max_overflow=8)
    old_factory, old_engine = db.SessionLocal, db.engine
    try:
        db.Base.metadata.create_all(engine)
        # Intentionally wrong migration order must roll back cleanly.
        with pytest.raises(Exception):
            _migration(engine, "I04_KNOWLEDGE_BASE_V1_POSTGRES.sql")
        assert "intelligence_knowledge" not in inspect(engine).get_table_names()
        _migration(engine, "F01_GLOBAL_FENCING_V1_POSTGRES.sql")
        _migration(engine, "I04_KNOWLEDGE_BASE_V1_POSTGRES.sql")
        _migration(engine, "I20_PUBLIC_RESEARCH_COORDINATION_V1_POSTGRES.sql")
        # Repeatable formal migration must preserve state, no duplicate rows.
        _migration(engine, "I20_PUBLIC_RESEARCH_COORDINATION_V1_POSTGRES.sql")
        db.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        db.engine = engine
        yield engine
    finally:
        db.engine = old_engine
        db.SessionLocal = old_factory
        CoordinationMetadata.metadata.drop_all(engine)
        db.KnowledgeMetadata.metadata.drop_all(engine)
        with engine.begin() as con:
            con.exec_driver_sql("DROP TABLE IF EXISTS real_authorization_fences")
            con.exec_driver_sql("DROP TABLE IF EXISTS real_global_authorization_fences")
            con.exec_driver_sql("DROP TABLE IF EXISTS nexora_schema_revisions")
        db.Base.metadata.drop_all(engine)
        engine.dispose()


def _add(index, now=CLOCK):
    return coord.enqueue_public_task(
        task_key=f"research_batch_{index:04d}",
        market="public", symbol="SHARED",
        source={
            "source_version": "public_i03_v1",
            "closed_epoch": 20_000 + index,
            "timeframe_seconds": 60,
        }, now=now,
    )


def test_formal_f01_i04_queue_migration_and_schema_constraints(pg):
    inspector = inspect(pg)
    assert "intelligence_knowledge" in inspector.get_table_names()
    assert "public_research_tasks" in inspector.get_table_names()
    with pg.begin() as con:
        names = set(con.execute(text("SELECT revision FROM nexora_schema_revisions")).scalars())
        assert names == {
            "f01_global_fencing_v1", "i04_knowledge_base_v1",
            "i20_public_research_coordination_v1",
        }
        assert con.execute(text("SELECT count(*) FROM public_research_capacity")).scalar() == 1
    assert set(CoordinationMetadata.metadata.tables).isdisjoint(db.Base.metadata.tables)


def test_no_ddl_or_dml_on_postgresql_application_startup(pg):
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith((
            "CREATE ", "ALTER ", "DROP ", "INSERT ", "UPDATE ", "DELETE ",
            "TRUNCATE ",
        )):
            statements.append(statement[:100])
    event.listen(pg, "before_cursor_execute", capture)
    try:
        db.init_db()
        assert not statements
    finally:
        event.remove(pg, "before_cursor_execute", capture)


def test_shared_capacity_and_idempotent_public_enqueue(pg):
    x = _add(1)
    y = _add(1)
    assert x["created"] is True
    assert y == {"id": x["id"], "state": "PENDING", "created": False}
    with pytest.raises(ValueError, match="collision"):
        coord.enqueue_public_task(
            task_key="research_batch_0001", market="public", symbol="OTHER",
            source={"source_version": "public_i03_v1",
                    "closed_epoch": 20_000, "timeframe_seconds": 60}, now=CLOCK,
        )
    assert coord.set_public_dynamic_limit(new_limit=3, now=CLOCK)["dynamic_limit"] == 3
    for cap in (0, 9, True, -1):
        with pytest.raises(ValueError):
            coord.set_public_dynamic_limit(new_limit=cap, now=CLOCK)


def test_multiple_threads_never_exceed_persisted_capacity(pg):
    for ix in range(2, 30):
        _add(ix)
    coord.set_public_dynamic_limit(new_limit=3, now=CLOCK)
    with ThreadPoolExecutor(max_workers=15) as pool:
        claimed = list(pool.map(
            lambda i: coord.claim_public_task(
                worker_id=f"thread_{i:04d}", now=CLOCK,
            ), range(15),
        ))
    leases = [x for x in claimed if x]
    assert len(leases) == 3
    assert len({i["id"] for i in leases}) == 3
    metrics = coord.public_worker_metrics(now=CLOCK)
    assert metrics["active_leases"] == 3
    assert metrics["effective_capacity"] == 3
    assert metrics["hard_cap"] == 8


def _child_claim(pg_url, worker_id, clock, output):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import db
    from app.intelligence2 import public_research_coordinator as coord
    engine = create_engine(pg_url, pool_pre_ping=True)
    db.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        out = coord.claim_public_task(worker_id=worker_id, now=clock)
        output.put(out)
    finally:
        engine.dispose()


def test_distinct_processes_obey_same_cap_and_no_duplicate_claim(pg):
    # Prior test left 3 valid leases; expire their leases and recover before
    # a fresh independent multiprocess cap check.
    t = CLOCK + timedelta(seconds=coord.LEASE_SECONDS + 3)
    coord.set_public_dynamic_limit(new_limit=2, now=t)
    context = mp.get_context("spawn")
    out = context.Queue()
    procs = [
        context.Process(
            target=_child_claim, args=(PG, f"proc_{i:04d}", t, out),
        ) for i in range(6)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
        assert p.exitcode == 0
    results = [out.get(timeout=5) for _ in procs]
    accepted = [x for x in results if x]
    assert len(accepted) == 2
    assert len({x["id"] for x in accepted}) == 2
    assert coord.public_worker_metrics(now=t)["active_leases"] == 2


def test_fence_rejects_late_worker_after_restart_and_allows_new_owner(pg):
    t = CLOCK + timedelta(seconds=2*coord.LEASE_SECONDS + 10)
    coord.set_public_dynamic_limit(new_limit=1, now=t)
    old = coord.claim_public_task(worker_id="owner_0001", now=t)
    assert old is not None
    after_expiry = t + timedelta(seconds=coord.LEASE_SECONDS + 2)
    refreshed = coord.claim_public_task(
        worker_id="owner_0002", now=after_expiry,
    )
    assert refreshed is not None
    assert refreshed["fence"] >= 1
    if refreshed["id"] == old["id"]:
        assert refreshed["fence"] == old["fence"] + 1
    stale = coord.finish_public_task(
        task_id=old["id"], worker_id=old["worker_id"], fence=old["fence"],
        duration_ms=10, result_digest="a"*64, now=after_expiry,
    )
    assert stale is False
    assert coord.finish_public_task(
        task_id=refreshed["id"], worker_id=refreshed["worker_id"],
        fence=refreshed["fence"], result_digest="b"*64, duration_ms=22,
        now=after_expiry + timedelta(seconds=1),
    ) is True


def test_error_retry_and_persisted_worker_metrics(pg):
    t = CLOCK + timedelta(seconds=5*coord.LEASE_SECONDS)
    coord.set_public_dynamic_limit(new_limit=1, now=t)
    work = coord.claim_public_task(worker_id="retry_0001", now=t)
    assert work is not None
    assert coord.finish_public_task(
        task_id=work["id"], worker_id=work["worker_id"],
        fence=work["fence"], duration_ms=500,
        error_code="PUBLIC_RESEARCH_UNAVAILABLE",
        now=t + timedelta(seconds=1),
    )
    another = coord.claim_public_task(
        worker_id="retry_0002", now=t + timedelta(seconds=25),
    )
    assert another is not None
    assert coord.public_worker_metrics(now=t + timedelta(seconds=25))[
        "p95_worker_ms_last_1000"
    ] is not None
    assert coord.suggest_public_dynamic_limit(
        current=8, error_fraction=.2, p95_ms=4000,
    ) == 4
    assert coord.suggest_public_dynamic_limit(
        current=8, error_fraction=0, p95_ms=100,
    ) == 8


def test_request_budget_caps_shared_research_claims(pg):
    # Advance a clock window to clear previous leases, and request 65
    # distinct completed tasks sequentially, without an actual Deriv call.
    t = CLOCK + timedelta(days=1)
    coord.set_public_dynamic_limit(new_limit=8, now=t)
    for i in range(100, 170):
        _add(i, now=t)
    successful = 0
    for i in range(65):
        claim = coord.claim_public_task(worker_id=f"budget_{i:04d}", now=t)
        if claim is None:
            break
        successful += 1
        assert coord.finish_public_task(
            task_id=claim["id"], worker_id=claim["worker_id"],
            fence=claim["fence"], duration_ms=1, result_digest="c"*64,
            now=t+timedelta(seconds=1),
        )
    assert successful <= coord.MAX_REQUESTS_PER_MINUTE
    assert coord.public_worker_metrics(now=t)["requests_used_current_window"] <= 60


def test_atomic_snapshot_delivery_rejects_stale_worker_and_is_idempotent(pg):
    from app.intelligence2 import research_orchestrator
    now = CLOCK + timedelta(days=2)
    coord.set_public_dynamic_limit(new_limit=1, now=now)
    request = coord.enqueue_public_task(
        task_key="research_batch_fenced_snapshot",
        market="public", symbol="SHARED",
        source={
            "source_version": "public_i03_v1",
            "closed_epoch": 50_000,
            "timeframe_seconds": 60,
        }, now=now,
    )
    claim = coord.claim_public_task(worker_id="fenced_worker", now=now)
    assert claim is not None
    source = {
        "version": research_orchestrator.VERSION,
        "assets": [{
            "market": "forex", "symbol": "FRXEURUSD",
            "state": "RESEARCH_READY",
            "observed_trend": "UP", "context_consensus": "THREE_TIMEFRAMES",
            "evidence_strength": "CONSISTENT",
            "evidence_completeness_points": 100,
            "fingerprint": "a"*64,
            "valid_until_epoch": int(now.timestamp()) + 60*10,
            "reason_codes": ["PUBLIC_DATA_READY_FOR_STUDY"],
            "execution_allowed": False,
            "approved_for_trading": False,
        }],
        "distribution": {"publish_allowed": False},
        "orders_sent": 0, "execution_allowed": False,
        "approved_for_trading": False,
    }
    view = decisions.prioritize_public_research(
        source, now=int(now.timestamp()),
    )
    after = now + timedelta(seconds=coord.LEASE_SECONDS+2)
    outdated = decisions.persist_public_research_snapshot(
        view, now=after, claim=claim, duration_ms=42,
    )
    assert outdated["stale"] is True
    with db.SessionLocal() as session:
        assert session.query(HistoricalResearchReport).filter(
            HistoricalResearchReport.research_key.like("I20_PUBLIC_RESEARCH_%")
        ).count() == 0
    replacement = coord.claim_public_task(worker_id="second_worker", now=after)
    assert replacement is not None
    saved = decisions.persist_public_research_snapshot(
        view, now=after+timedelta(seconds=1),
        claim=replacement, duration_ms=47,
    )
    assert saved["stale"] is False and saved["created"] is True
    again = decisions.persist_public_research_snapshot(
        view, now=after+timedelta(seconds=2),
    )
    assert again["created"] is False and again["id"] == saved["id"]
    with db.SessionLocal() as session:
        task = session.get(ResearchTask, replacement["id"])
        assert task.status == "DONE"
        assert task.worker_id is None and task.lease_until is None
        assert task.research_digest == saved["digest"]
    assert coord.finish_public_task(
        task_id=replacement["id"], worker_id=replacement["worker_id"],
        fence=replacement["fence"], duration_ms=12,
        result_digest="f"*64, now=after+timedelta(seconds=4),
    ) is False


def test_no_financial_runtime_registration_or_trigger(pg):
    from app.intelligence2 import public_research_decisions as public
    import asyncio
    assert asyncio.run(public.run_one_public_research_task(
        worker_id="disabled_worker",
    )) == {
        "state": "DISTRIBUTED_PUBLIC_RESEARCH_DISABLED",
        "execution_allowed": False,
    }

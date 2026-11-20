"""I04 disposable PostgreSQL 16 migration and transaction contract tests.

Strictly restricted to localhost CI database and isolated temporary schemas.
Never touches production Supabase or Render; drops every test schema.
"""
from __future__ import annotations

import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app import db
from app.intelligence2 import knowledge_base as kb

ROOT = Path(__file__).resolve().parents[1]
F01 = (ROOT/"migrations"/"F01_GLOBAL_FENCING_V1_POSTGRES.sql").read_text()
I04 = (ROOT/"migrations"/"I04_KNOWLEDGE_BASE_V1_POSTGRES.sql").read_text()
NOW = datetime(2026,11,20,12,0,tzinfo=timezone.utc)


def _safe_dsn():
    if os.getenv("NEXORA_P0_EPHEMERAL_PG") != "yes":
        pytest.skip("I04 PostgreSQL migration only on disposable CI service")
    raw = os.environ["NEXORA_P0_TEST_DATABASE_URL"]
    url = make_url(raw)
    if not (url.drivername=="postgresql+psycopg"
            and url.host in ("localhost","127.0.0.1") and url.port==5432
            and url.database=="nexora_p0_ci" and url.username=="p0_ci"):
        pytest.fail("I04 refuses non-disposable PostgreSQL DSN")
    assert os.getenv("NEXORA_REAL_AUTOPILOT_ENABLED","false")=="false"
    assert os.getenv("NEXORA_REAL_RELEASE_APPROVED","false")=="false"
    return raw


@pytest.fixture
def isolated(monkeypatch):
    dsn = _safe_dsn()
    admin=create_engine(dsn,pool_pre_ping=True)
    schema="kb04_"+uuid.uuid4().hex[:12]
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine=create_engine(
        dsn,pool_pre_ping=True,
        connect_args={"options":f"-c search_path={schema}"})
    monkeypatch.setattr(db,"engine",engine)
    monkeypatch.setattr(db,"SessionLocal",sessionmaker(bind=engine,expire_on_commit=False))
    try:
        yield dsn,schema,engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _apply(dsn,schema,sql):
    psycopg_dsn=dsn.replace("postgresql+psycopg://","postgresql://",1)
    with psycopg.connect(
        psycopg_dsn,autocommit=True,options=f"-c search_path={schema}"
    ) as connection:
        connection.execute(sql,prepare=False)


def _create_f01(isolated, *, all_tables=False):
    dsn,schema,engine=isolated
    if all_tables:
        db.Base.metadata.create_all(engine)
    else:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
    _apply(dsn,schema,F01)


def test_additive_i04_migration_only_after_f01_and_idempotent(isolated):
    dsn,schema,engine=isolated
    with pytest.raises(Exception):
        _apply(dsn,schema,I04)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('intelligence_knowledge')")).scalar() is None
    _create_f01(isolated)
    _apply(dsn,schema,I04)
    with engine.connect() as conn:
        count=conn.execute(text(
            "SELECT count(*) FROM nexora_schema_revisions "
            "WHERE revision='i04_knowledge_base_v1'")).scalar_one()
        assert count==1
        tables=conn.execute(text(
            "SELECT to_regclass('intelligence_knowledge'), "
            "to_regclass('intelligence_knowledge_evidence'), "
            "to_regclass('intelligence_knowledge_decisions')")).one()
        assert all(tables)
    created=kb.discover(
        category="MARKET",market="forex",symbol="frxEURUSD",
        timeframe_seconds=900,subject="public_regime",
        model_version="i03_v1",regime="UPTREND",
        attributes={"regime":"UPTREND","sample_size":144},
        valid_from=NOW-timedelta(days=1),
        valid_until=NOW+timedelta(days=7),
        next_review_at=NOW+timedelta(days=1),
        now=NOW)
    _apply(dsn,schema,I04)
    assert kb.get_knowledge(created["id"],now=NOW)["revision"]==1
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT count(*) FROM nexora_schema_revisions "
            "WHERE revision='i04_knowledge_base_v1'")).scalar_one()==1


def test_incompatible_partial_schema_rolls_back_entire_i04_migration(isolated):
    dsn,schema,engine=isolated
    _create_f01(isolated)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE intelligence_knowledge (id INTEGER PRIMARY KEY)"))
    with pytest.raises(Exception,match="I04 incompatible|I04 table"):
        _apply(dsn,schema,I04)
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT to_regclass('intelligence_knowledge_evidence')")).scalar() is None
        assert conn.execute(text(
            "SELECT to_regclass('intelligence_knowledge_decisions')")).scalar() is None
        assert conn.execute(text(
            "SELECT count(*) FROM nexora_schema_revisions "
            "WHERE revision='i04_knowledge_base_v1'")).scalar_one()==0


def test_postgres_startup_never_bootstraps_i04_and_reads_revision_only(
    isolated,monkeypatch,
):
    dsn,schema,engine=isolated
    _create_f01(isolated,all_tables=True)
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):
        statements.append(statement)
    event.listen(engine,"before_cursor_execute",capture)
    try:
        monkeypatch.setenv("NEXORA_INTELLIGENCE_KNOWLEDGE_ENABLED","false")
        db.init_db()  # F01 baseline must remain usable without I04 stamp.
        monkeypatch.setenv("NEXORA_INTELLIGENCE_KNOWLEDGE_ENABLED","true")
        statements.clear()
        with pytest.raises(RuntimeError,match="Migração formal I04 obrigatória"):
            db.init_db()
        assert not any(re.match(r"^\s*(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|TRUNCATE)\b",
                                q,re.I) for q in statements),statements
        event.remove(engine,"before_cursor_execute",capture)
        _apply(dsn,schema,I04)
        event.listen(engine,"before_cursor_execute",capture)
        statements.clear()
        db.init_db()
        assert not any(re.match(r"^\s*(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|TRUNCATE)\b",
                                q,re.I) for q in statements),statements
    finally:
        try: event.remove(engine,"before_cursor_execute",capture)
        except Exception: pass


def test_missing_i04_table_blocks_enabled_startup_with_zero_ddl(
    isolated,monkeypatch,
):
    dsn,schema,engine=isolated
    _create_f01(isolated,all_tables=True)
    _apply(dsn,schema,I04)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE intelligence_knowledge_decisions"))
    monkeypatch.setenv("NEXORA_INTELLIGENCE_KNOWLEDGE_ENABLED","true")
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):
        statements.append(statement)
    event.listen(engine,"before_cursor_execute",capture)
    try:
        with pytest.raises(RuntimeError,match="migração formal obrigatória"):
            db.init_db()
        assert not any(re.match(r"^\s*(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE)\b",
                                q,re.I) for q in statements)
    finally:
        event.remove(engine,"before_cursor_execute",capture)


def test_concurrent_postgres_cas_single_winner_and_no_revision_gaps(
    isolated,
):
    dsn,schema,engine=isolated
    _create_f01(isolated,all_tables=True)
    _apply(dsn,schema,I04)
    item=kb.discover(
        category="SIGNAL",market="derived",symbol="R_100",
        timeframe_seconds=60,subject="public_pattern",
        model_version="i03_v1",regime="UPTREND",
        attributes={"signal_pattern":"HIGHER_HIGHS","sample_size":144},
        valid_from=NOW-timedelta(days=1),
        valid_until=NOW+timedelta(days=7),
        next_review_at=NOW+timedelta(days=1),
        now=NOW,
    )
    def worker(reason):
        try:
            return kb.change_state(
                knowledge_id=item["id"],expected_revision=1,
                target_state="UNDER_VALIDATION",reason_code=reason,now=NOW,
            )["revision"]
        except kb.KnowledgeConflict:
            return "STALE"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(worker,("REVIEW_A","REVIEW_B")))
    assert sorted(map(str,outcomes))==["2","STALE"]
    latest=kb.get_knowledge(item["id"],now=NOW)
    assert latest["revision"]==2
    assert [row["revision"] for row in latest["decisions"]]==[1,2]
    assert latest["state"]=="UNDER_VALIDATION"

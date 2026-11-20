"""I05 research release concurrency on disposable PG16 ONLY.

Production DSNs rejected. No Deriv connection or financial action.
Each test creates and drops an isolated schema in localhost CI PostgreSQL.
"""
from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, text, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app import db
from app.intelligence2 import learning_engine as eng, learning_registry as reg
from app.intelligence2 import knowledge_base as kb

NOW=datetime(2026,11,20,12,0,tzinfo=timezone.utc)
APPROVED_AT=NOW+timedelta(hours=6)
ROOT=Path(__file__).resolve().parents[1]
F01=(ROOT/"migrations"/"F01_GLOBAL_FENCING_V1_POSTGRES.sql").read_text()
I04=(ROOT/"migrations"/"I04_KNOWLEDGE_BASE_V1_POSTGRES.sql").read_text()


def _safe_dsn():
    if os.getenv("NEXORA_P0_EPHEMERAL_PG")!="yes":
        pytest.skip("I05 PG tests require disposable localhost Postgres")
    raw=os.environ["NEXORA_P0_TEST_DATABASE_URL"]
    url=make_url(raw)
    if not (
        url.drivername=="postgresql+psycopg"
        and url.host in ("127.0.0.1","localhost")
        and url.port==5432 and url.database=="nexora_p0_ci"
        and url.username=="p0_ci"
    ):
        pytest.fail("Non-disposable DB rejected")
    return raw


@pytest.fixture
def isolated(monkeypatch):
    dsn=_safe_dsn()
    admin=create_engine(dsn,pool_pre_ping=True)
    schema="i05_research_"+uuid.uuid4().hex[:10]
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine=create_engine(
        dsn,pool_pre_ping=True,
        connect_args={"options":f"-c search_path={schema}"})
    monkeypatch.setattr(db,"engine",engine)
    monkeypatch.setattr(db,"SessionLocal",sessionmaker(bind=engine,expire_on_commit=False))
    monkeypatch.setenv("NEXORA_INTELLIGENCE_KNOWLEDGE_ENABLED","true")
    monkeypatch.setenv("NEXORA_INTELLIGENCE_LEARNING_ENABLED","true")
    monkeypatch.setenv("NEXORA_INTELLIGENCE_LEARNING_PROMOTION_ENABLED","true")
    monkeypatch.setenv("NEXORA_REAL_AUTOPILOT_ENABLED","false")
    monkeypatch.setenv("NEXORA_REAL_RELEASE_APPROVED","false")
    # Disposable fixture setup. Neither migration nor any startup operation
    # is ever called on the production database.
    db.Base.metadata.create_all(engine)
    pg_dsn=dsn.replace("postgresql+psycopg://","postgresql://",1)
    with psycopg.connect(pg_dsn,autocommit=True,
                         options=f"-c search_path={schema}") as conn:
        conn.execute(F01,prepare=False)
        conn.execute(I04,prepare=False)
    db.init_db()  # Only SELECTs; formal migrations already applied.
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _history(sequence):
    tf=60
    size=eng.MIN_HISTORY-eng.MIN_BARS+1
    last=(int(NOW.timestamp())//tf-1)*tf
    return {
        "version":eng.SOURCE_VERSION,"market":"derived","symbol":"R_100",
        "timeframe_seconds":tf,"observed_at_epoch":int(NOW.timestamp()),
        "data_quality":"COMPLETE","context_version":eng.CONTEXT_VERSION,
        "i01_last_closed_epoch":last,"execution_allowed":False,
        "observations":[
            {"closed_epoch":last-(size-1-i)*tf,
             "regime":sequence[i%len(sequence)]}
            for i in range(size)
        ],
    }


def _history_at(sequence, now):
    history=_history(sequence)
    shift=int((now-NOW).total_seconds())
    history["observed_at_epoch"]+=shift
    history["i01_last_closed_epoch"]+=shift
    for row in history["observations"]:
        row["closed_epoch"]+=shift
    return history


def _active_market():
    row=kb.discover(
        category="MARKET",market="derived",symbol="R_100",
        subject="pg_public_learning",model_version="i03_v1",
        timeframe_seconds=60,regime="UNKNOWN",
        attributes={"regime":"UPTREND","sample_size":1000},
        valid_from=NOW-timedelta(days=1),
        valid_until=NOW+timedelta(days=30),
        next_review_at=NOW+timedelta(days=7),now=NOW)
    row=kb.change_state(
        knowledge_id=row["id"],expected_revision=row["revision"],
        target_state="UNDER_VALIDATION",reason_code="PG_PUBLIC_RESEARCH",now=NOW)
    for idx in (1,2):
        row=kb.append_evidence(
            knowledge_id=row["id"],expected_revision=row["revision"],
            source_type="PUBLIC_CONTEXT",
            source_ref=eng._sha({"pg_public_ref":idx}),
            source_digest=eng._sha({"pg_public_digest":idx}),
            origin_group=f"PG_PUBLIC_SOURCE_{idx}",
            observation_count=150,period_from=NOW-timedelta(hours=12),
            period_to=NOW-timedelta(hours=1),
            verified=True,summary={"sample_size":150},now=NOW)
    row=kb.change_state(
        knowledge_id=row["id"],expected_revision=row["revision"],
        target_state="VALIDATED",reason_code="PG_VALIDATED_RESEARCH",now=NOW)
    return kb.change_state(
        knowledge_id=row["id"],expected_revision=row["revision"],
        target_state="ACTIVE",reason_code="PG_RESEARCH_ACTIVE",now=NOW)


def _validated_model(source,sequence):
    stage=reg.stage_public_candidate(_history(sequence),
                                      market_knowledge_id=source["id"],now=NOW)
    assert stage["state"]=="UNDER_VALIDATION"
    independent=reg.append_independent_public_validation(
        model_knowledge_id=stage["knowledge_id"],
        model_digest=stage["report"]["model_id_sha256"],
        expected_revision=4,
        history=_history_at(sequence,APPROVED_AT),now=APPROVED_AT)
    assert independent["verified"] is True
    accepted=reg.validate_research_challenger(
        model_knowledge_id=stage["knowledge_id"],
        model_digest=stage["report"]["model_id_sha256"],
        expected_revision=independent["revision"],now=APPROVED_AT)
    assert accepted["state"]=="VALIDATED"
    return stage,accepted


def test_postgresql_two_concurrent_public_champions_single_cas_winner(isolated):
    source=_active_market()
    first,a=_validated_model(source,(
        "UPTREND","DOWNTREND","RANGING","TRANSITION"))
    second,b=_validated_model(source,(
        "RANGING","UPTREND","TRANSITION","DOWNTREND"))
    jobs=[(first,a),(second,b)]

    def approve(pair):
        stage,validated=pair
        try:
            result=reg.promote_research_champion(
                model_knowledge_id=stage["knowledge_id"],
                model_digest=stage["report"]["model_id_sha256"],
                expected_model_revision=validated["revision"],
                expected_release_id=None,
                approval_reason="PG_EXPLICIT_RESEARCH_RELEASE",now=APPROVED_AT)
            return "PROMOTED",result["id"],stage["knowledge_id"]
        except kb.KnowledgeConflict:
            return "STALE",None,stage["knowledge_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(approve,jobs))
    assert sorted(x[0] for x in results)==["PROMOTED","STALE"]
    champion_id=next(x[2] for x in results if x[0]=="PROMOTED")
    persisted=reg.get_research_release(
        market="derived",symbol="R_100",
        timeframe_seconds=60,now=APPROVED_AT)
    assert persisted["champion_knowledge_id"]==champion_id
    assert persisted["generation"]==1
    assert persisted["execution_allowed"] is False
    with db.SessionLocal() as session:
        events=session.scalars(select(db.IntelligenceKnowledgeDecision).where(
            db.IntelligenceKnowledgeDecision.knowledge_id==champion_id,
        ).order_by(db.IntelligenceKnowledgeDecision.revision)).all()
        assert events[-1].decision=="I05_RESEARCH_ONLY_PROMOTION"
        assert [e.revision for e in events]==list(range(1,len(events)+1))


def test_pg_restart_research_release_readonly_and_no_new_schema(isolated,monkeypatch):
    source=_active_market()
    stage,valid=_validated_model(
        source,("UPTREND","DOWNTREND","RANGING","TRANSITION"))
    release=reg.promote_research_champion(
        model_knowledge_id=stage["knowledge_id"],
        model_digest=stage["report"]["model_id_sha256"],
        expected_model_revision=valid["revision"],expected_release_id=None,
        approval_reason="PG_RESTART_VERIFIED",now=APPROVED_AT)
    one=reg.get_research_release(
        market="derived",symbol="R_100",timeframe_seconds=60,now=APPROVED_AT)
    assert one["release_id"]==release["id"]
    original=db.SessionLocal
    db.SessionLocal=sessionmaker(bind=isolated,expire_on_commit=False)
    other=reg.get_research_release(
        market="derived",symbol="R_100",timeframe_seconds=60,now=APPROVED_AT)
    assert other==one
    with isolated.connect() as conn:
        existing={name for (name,) in conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()"
        ))}
        assert not any(name.startswith("intelligence_learning_") for name in existing)
        assert {"historical_research_reports","intelligence_knowledge",
                "intelligence_knowledge_evidence",
                "intelligence_knowledge_decisions"}<=existing

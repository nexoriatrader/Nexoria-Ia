"""I05 public research training/governance tests — no financial execution."""
from __future__ import annotations

# Original I05 PR #56 persistence lifecycle tests, adapted to separate
# I04 metadata for disposable SQLite/PostgreSQL; no execution.

import asyncio
import copy
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import db, market, real_broker
from app.intelligence2 import (
    learning_engine as eng,
    learning_registry as reg,
    learning_cycle as cycle,
    knowledge_base as kb,
)
from app.research_persistence import HistoricalResearchReport
from app.research_retention import cleanup_research_reports

NOW=datetime(2026,11,20,12,0,tzinfo=timezone.utc)
APPROVED_AT=NOW+timedelta(hours=6)
TF=60
SYMBOL="R_100"
MARKET="derived"


@pytest.fixture
def store(tmp_path,monkeypatch):
    url=os.getenv("NEXORA_CONSOLIDATION_TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/i05.sqlite"
    engine=create_engine(url)
    db.Base.metadata.create_all(engine)
    db.KnowledgeMetadata.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,"SessionLocal",factory)
    monkeypatch.setenv("NEXORA_INTELLIGENCE_KNOWLEDGE_ENABLED","true")
    monkeypatch.setenv("NEXORA_INTELLIGENCE_LEARNING_ENABLED","true")
    monkeypatch.setenv("NEXORA_INTELLIGENCE_LEARNING_PROMOTION_ENABLED","false")
    try:
        yield factory
    finally:
        if engine.dialect.name == "postgresql":
            db.KnowledgeMetadata.metadata.drop_all(engine)
            db.Base.metadata.drop_all(engine)
        engine.dispose()


def _history(*,now=NOW,sequence=None, extra=None):
    order=sequence or ("UPTREND","DOWNTREND","RANGING","TRANSITION")
    last=(int(now.timestamp())//TF-1)*TF
    size=eng.MIN_HISTORY-eng.MIN_BARS+1
    observations=[
        {"closed_epoch":last-(size-1-i)*TF,
         "regime":order[i%len(order)]}
        for i in range(size)
    ]
    report={
        "version":eng.SOURCE_VERSION,
        "market":MARKET,"symbol":SYMBOL,"timeframe_seconds":TF,
        "observed_at_epoch":int(now.timestamp()),
        "data_quality":"COMPLETE",
        "context_version":eng.CONTEXT_VERSION,
        "i01_last_closed_epoch":last,
        "observations":observations,
        "execution_allowed":False,
    }
    if extra:
        report.update(extra)
    return report


def _market_knowledge(now=NOW,regime="UNKNOWN"):
    row=kb.discover(
        category="MARKET",market=MARKET,symbol=SYMBOL,
        timeframe_seconds=TF,subject="public_regime_observation",
        model_version="i03_context_v1",regime=regime,
        attributes={"regime":"UPTREND","data_quality":"COMPLETE",
                    "sample_size":1000},
        valid_from=now-timedelta(days=3),
        valid_until=now+timedelta(days=40),
        next_review_at=now+timedelta(days=10),now=now,
    )
    row=kb.change_state(knowledge_id=row["id"],expected_revision=row["revision"],
                        target_state="UNDER_VALIDATION",
                        reason_code="PUBLIC_VERIFICATION",now=now)
    for source in ("independent_a","independent_b"):
        row=kb.append_evidence(
            knowledge_id=row["id"],expected_revision=row["revision"],
            source_type="PUBLIC_CONTEXT",
            source_ref=eng._sha({"source":source,"ref":1}),
            source_digest=eng._sha({"source":source,"digest":2}),
            origin_group=source.upper(),
            observation_count=150,period_from=now-timedelta(days=1),
            period_to=now-timedelta(hours=1),verified=True,
            summary={"sample_size":150,"data_quality":"COMPLETE"},now=now)
    row=kb.change_state(knowledge_id=row["id"],expected_revision=row["revision"],
                        target_state="VALIDATED",reason_code="PUBLIC_EVIDENCE_OK",now=now)
    return kb.change_state(knowledge_id=row["id"],expected_revision=row["revision"],
                           target_state="ACTIVE",reason_code="RESEARCH_ACTIVE",now=now)


def _staged_validated(*, market_source, now=NOW, sequence=None):
    history=_history(now=now,sequence=sequence)
    stage=reg.stage_public_candidate(
        history,market_knowledge_id=market_source["id"],now=now)
    assert stage["state"]=="UNDER_VALIDATION"
    assert stage["report"]["research_eligible"] is True
    later=now+timedelta(hours=6)
    independent=reg.append_independent_public_validation(
        model_knowledge_id=stage["knowledge_id"],
        model_digest=stage["report"]["model_id_sha256"],
        expected_revision=4,
        history=_history(now=later,sequence=sequence),now=later)
    assert independent["verified"] is True
    evaluated=reg.validate_research_challenger(
        model_knowledge_id=stage["knowledge_id"],
        model_digest=stage["report"]["model_id_sha256"],
        expected_revision=independent["revision"],now=later)
    assert evaluated["state"]=="VALIDATED"
    return stage,evaluated


def test_training_strictly_uses_closed_public_regimes_and_holdout():
    history=_history()
    result=eng.evaluate_public_regimes(history)
    assert result["research_eligible"] is True
    assert result["train_samples"]>=eng.MIN_TRAIN
    assert result["holdout_samples"]>=eng.MIN_HOLDOUT
    assert result["embargo_bars"]==eng.MIN_BARS
    assert result["train_until_epoch"]<result["holdout_from_epoch"]
    assert result["holdout_accuracy"]==1.0
    assert result["baseline_accuracy"]==0.0
    assert result["wilson95_lower"]>.9
    assert result["holdout_half_lifts"]==[1.0,1.0]
    assert len(result["model_id_sha256"])==64
    assert result["execution_allowed"] is False
    assert result["approved_for_trading"] is False
    decision=eng.select_research_roles(candidate=result)
    assert decision["promotion_performed"] is False
    assert decision["champion"] is None


@pytest.mark.parametrize("mutation",[
    {"user_id":200},{"balance":500},{"account_id":"CR123"},
    {"observed_at_epoch":123},{"data_quality":"GAPPED"},
    {"context_version":"i00_fake"},
])
def test_malformed_private_untrusted_or_stale_series_rejected(mutation):
    with pytest.raises(eng.LearningError):
        eng.evaluate_public_regimes(_history(extra=mutation))


def test_open_candle_gap_and_fictitious_regime_fail_closed():
    series=_history()
    series["observations"][-1]["closed_epoch"]+=60
    with pytest.raises(eng.LearningError):
        eng.evaluate_public_regimes(series)
    series=_history()
    series["observations"][99]["regime"]="PROFIT_SIGNAL"
    with pytest.raises(eng.LearningError):
        eng.evaluate_public_regimes(series)
    series=_history()
    series["observations"][99]["closed_epoch"]-=60
    with pytest.raises(eng.LearningError):
        eng.evaluate_public_regimes(series)


def test_no_oos_lift_prohibits_staging_and_promotion(store):
    source=_market_knowledge()
    constant=_history(sequence=("RANGING",))
    result=eng.evaluate_public_regimes(constant)
    assert result["holdout_accuracy"]==result["baseline_accuracy"]==1.0
    assert result["research_eligible"] is False
    staged=reg.stage_public_candidate(
        constant,market_knowledge_id=source["id"],now=NOW)
    assert staged["state"]=="REJECTED_OOS"
    assert staged["knowledge_id"] is None
    with store() as session:
        assert session.scalars(select(db.IntelligenceKnowledge).where(
            db.IntelligenceKnowledge.category=="MODEL")).all()==[]


def test_stage_persists_model_metadata_and_two_public_windows_no_user_data(store):
    source=_market_knowledge()
    history=_history()
    staged=reg.stage_public_candidate(
        history,market_knowledge_id=source["id"],now=NOW)
    repeat=reg.stage_public_candidate(
        history,market_knowledge_id=source["id"],now=NOW)
    assert repeat["artifact_id"]==staged["artifact_id"]
    assert repeat["knowledge_id"]==staged["knowledge_id"]
    assert repeat["state"]=="UNDER_VALIDATION"
    assert staged["report"]["research_eligible"] is True
    assert staged["execution_allowed"] is False
    knowledge=kb.get_knowledge(staged["knowledge_id"],now=NOW)
    assert knowledge["state"]=="UNDER_VALIDATION"
    assert len(knowledge["evidence"])==2
    assert {e["origin_group"] for e in knowledge["evidence"]} == {
        "PUBLIC_TRAIN_WINDOW","PUBLIC_HOLDOUT_WINDOW"}
    assert knowledge["revision"]==4
    with store() as session:
        artifact=session.get(HistoricalResearchReport,staged["artifact_id"])
        value=json.loads(artifact.payload_json)
        assert value["report_sha256"]==eng._sha(staged["report"])
        assert value["execution_allowed"] is False
        assert "account_id" not in artifact.payload_json
        assert "stake" not in artifact.payload_json
        assert "user_id" not in artifact.payload_json


def test_independent_second_public_holdout_is_required_and_embargoed(store):
    source=_market_knowledge()
    stage=reg.stage_public_candidate(_history(),
                                     market_knowledge_id=source["id"],now=NOW)
    with pytest.raises(eng.LearningError,match="Duas validações"):
        reg.validate_research_challenger(
            model_knowledge_id=stage["knowledge_id"],
            model_digest=stage["report"]["model_id_sha256"],
            expected_revision=4,now=NOW)
    with pytest.raises(eng.LearningError,match="insuficiente"):
        reg.append_independent_public_validation(
            model_knowledge_id=stage["knowledge_id"],
            model_digest=stage["report"]["model_id_sha256"],
            expected_revision=4,
            history=_history(now=NOW+timedelta(hours=3)),now=NOW+timedelta(hours=3))
    later=NOW+timedelta(hours=6)
    failed=reg.append_independent_public_validation(
        model_knowledge_id=stage["knowledge_id"],
        model_digest=stage["report"]["model_id_sha256"],
        expected_revision=4,
        history=_history(now=later,sequence=("UPTREND",)),now=later)
    assert failed["verified"] is False
    with pytest.raises(eng.LearningError,match="Duas validações"):
        reg.validate_research_challenger(
            model_knowledge_id=stage["knowledge_id"],
            model_digest=stage["report"]["model_id_sha256"],
            expected_revision=failed["revision"],now=later)


def test_validation_prohibits_unsafe_promotion_without_opt_in(store,monkeypatch):
    source=_market_knowledge()
    stage,evaluated=_staged_validated(market_source=source)
    with pytest.raises(eng.LearningError,match="desabilitada"):
        reg.promote_research_champion(
            model_knowledge_id=stage["knowledge_id"],
            model_digest=stage["report"]["model_id_sha256"],
            expected_model_revision=evaluated["revision"],
            expected_release_id=None,approval_reason="PUBLIC_OOS_APPROVED",now=NOW)
    model=kb.get_knowledge(stage["knowledge_id"],now=NOW)
    assert model["state"]=="VALIDATED"
    assert not model["reusable_for_research"]
    assert reg.get_research_release(market=MARKET,symbol=SYMBOL,
                                    timeframe_seconds=TF,now=NOW)["champion_knowledge_id"] is None


def test_controlled_research_champion_fallback_and_release_cas(store,monkeypatch):
    monkeypatch.setenv("NEXORA_INTELLIGENCE_LEARNING_PROMOTION_ENABLED","true")
    source=_market_knowledge()
    first,validated=_staged_validated(market_source=source)
    promoted=reg.promote_research_champion(
        model_knowledge_id=first["knowledge_id"],
        model_digest=first["report"]["model_id_sha256"],
        expected_model_revision=validated["revision"],
        expected_release_id=None,approval_reason="MANUAL_RESEARCH_APPROVAL",now=APPROVED_AT)
    assert promoted["champion_knowledge_id"]==first["knowledge_id"]
    assert promoted["fallback_knowledge_id"] is None
    assert promoted["execution_allowed"] is False
    assert kb.get_knowledge(first["knowledge_id"],now=APPROVED_AT)["state"]=="ACTIVE"
    with pytest.raises(kb.KnowledgeConflict):
        reg.promote_research_champion(
            model_knowledge_id=first["knowledge_id"],
            model_digest=first["report"]["model_id_sha256"],
            expected_model_revision=validated["revision"],
            expected_release_id=None,approval_reason="STALE_RELEASE",now=APPROVED_AT)
    second,second_validated=_staged_validated(
        market_source=source,sequence=(
            "RANGING","UPTREND","TRANSITION","DOWNTREND",
        ),
    )
    release=reg.promote_research_champion(
        model_knowledge_id=second["knowledge_id"],
        model_digest=second["report"]["model_id_sha256"],
        expected_model_revision=second_validated["revision"],
        expected_release_id=promoted["id"],approval_reason="SECOND_PUBLIC_APPROVAL",now=APPROVED_AT)
    assert release["champion_knowledge_id"]==second["knowledge_id"]
    assert release["fallback_knowledge_id"]==first["knowledge_id"]
    current=reg.get_research_release(
        market=MARKET,symbol=SYMBOL,timeframe_seconds=TF,now=APPROVED_AT)
    assert current["champion_knowledge_id"]==second["knowledge_id"]
    assert current["fallback_knowledge_id"]==first["knowledge_id"]
    assert current["execution_allowed"] is False
    assert current["generation"]==2


def test_future_dated_research_promotion_not_visible_before_approval(store,monkeypatch):
    monkeypatch.setenv("NEXORA_INTELLIGENCE_LEARNING_PROMOTION_ENABLED","true")
    source=_market_knowledge()
    candidate,validated=_staged_validated(market_source=source)
    reg.promote_research_champion(
        model_knowledge_id=candidate["knowledge_id"],
        model_digest=candidate["report"]["model_id_sha256"],
        expected_model_revision=validated["revision"],
        expected_release_id=None,approval_reason="TEST_CHRONOLOGY_GATE",
        now=APPROVED_AT)
    old_view=reg.get_research_release(
        market=MARKET,symbol=SYMBOL,timeframe_seconds=TF,now=NOW)
    assert old_view["state"]=="NO_VALIDATED_RESEARCH_MODEL"
    old_model=kb.get_knowledge(candidate["knowledge_id"],now=NOW)
    assert old_model["reusable_for_research"] is False
    assert old_model["reuse_status"]=="FUTURE_DECISION_TIMESTAMP"


def test_research_release_audit_tampering_fails_closed(store,monkeypatch):
    monkeypatch.setenv("NEXORA_INTELLIGENCE_LEARNING_PROMOTION_ENABLED","true")
    source=_market_knowledge()
    candidate,validated=_staged_validated(market_source=source)
    promoted=reg.promote_research_champion(
        model_knowledge_id=candidate["knowledge_id"],
        model_digest=candidate["report"]["model_id_sha256"],
        expected_model_revision=validated["revision"],
        expected_release_id=None,approval_reason="AUDITED_RESEARCH_RELEASE",
        now=APPROVED_AT)
    with store() as session:
        item=session.get(HistoricalResearchReport,promoted["id"])
        data=json.loads(item.payload_json)
        data["champion_knowledge_id"]=999999
        item.payload_json=json.dumps(data)
        session.commit()
    with pytest.raises(kb.KnowledgeUnavailable,match="Trilha"):
        reg.get_research_release(
            market=MARKET,symbol=SYMBOL,timeframe_seconds=TF,now=APPROVED_AT)


def test_open_contract_global_safety_barrier_blocks_model_promotion(store,monkeypatch):
    monkeypatch.setenv("NEXORA_INTELLIGENCE_LEARNING_PROMOTION_ENABLED","true")
    source=_market_knowledge()
    candidate,evaluated=_staged_validated(market_source=source)
    with store() as session:
        user=db.User(deriv_account_id="DEMO_MODEL_TEST")
        session.add(user);session.flush()
        session.add(db.DemoOrder(
            user_id=user.id,request_key="learning-safe-001",
            active_slot=user.id,state="UNKNOWN",symbol=SYMBOL,
            direction="CALL",duration_minutes=1,stake="1.00"))
        session.commit()
    with pytest.raises(eng.LearningError,match="OPEN_OPERATIONS"):
        reg.promote_research_champion(
            model_knowledge_id=candidate["knowledge_id"],
            model_digest=candidate["report"]["model_id_sha256"],
            expected_model_revision=evaluated["revision"],
            expected_release_id=None,approval_reason="MANUAL_RESEARCH_APPROVAL",now=APPROVED_AT)
    assert reg.get_research_release(
        market=MARKET,symbol=SYMBOL,timeframe_seconds=TF,now=NOW
    )["champion_knowledge_id"] is None


def test_auto_rollback_after_two_distinct_bad_public_windows(store,monkeypatch):
    monkeypatch.setenv("NEXORA_INTELLIGENCE_LEARNING_PROMOTION_ENABLED","true")
    source=_market_knowledge()
    first,first_valid=_staged_validated(market_source=source)
    r1=reg.promote_research_champion(
        model_knowledge_id=first["knowledge_id"],
        model_digest=first["report"]["model_id_sha256"],
        expected_model_revision=first_valid["revision"],
        expected_release_id=None,approval_reason="MANUAL_FIRST_CHAMPION",now=APPROVED_AT)
    second,second_valid=_staged_validated(
        market_source=source,sequence=(
            "RANGING","UPTREND","TRANSITION","DOWNTREND"
        ))
    reg.promote_research_champion(
        model_knowledge_id=second["knowledge_id"],
        model_digest=second["report"]["model_id_sha256"],
        expected_model_revision=second_valid["revision"],
        expected_release_id=r1["id"],approval_reason="MANUAL_SECOND_CHAMPION",now=APPROVED_AT)
    next1=APPROVED_AT+timedelta(hours=4)
    observation1=reg.monitor_and_rollback(
        market=MARKET,symbol=SYMBOL,timeframe_seconds=TF,
        history=_history(now=next1,sequence=("UPTREND",)),now=next1)
    assert observation1["state"]=="MONITORED"
    assert observation1["bad_streak"]==1
    assert observation1["champion_knowledge_id"]==second["knowledge_id"]
    replay=reg.monitor_and_rollback(
        market=MARKET,symbol=SYMBOL,timeframe_seconds=TF,
        history=_history(now=next1,sequence=("UPTREND",)),now=next1)
    assert replay["state"]=="AWAIT_NEW_PUBLIC_HOLDOUT"
    next2=APPROVED_AT+timedelta(hours=8)
    rollback=reg.monitor_and_rollback(
        market=MARKET,symbol=SYMBOL,timeframe_seconds=TF,
        history=_history(now=next2,sequence=("UPTREND",)),now=next2)
    assert rollback["state"]=="RESEARCH_ROLLED_BACK"
    assert rollback["champion_knowledge_id"]==first["knowledge_id"]
    latest=reg.get_research_release(
        market=MARKET,symbol=SYMBOL,timeframe_seconds=TF,now=next2)
    assert latest["champion_knowledge_id"]==first["knowledge_id"]
    assert latest["fallback_knowledge_id"] is None
    assert latest["release_event"]=="AUTO_ROLLBACK"
    assert latest["execution_allowed"] is False


def test_artifact_and_release_tampering_fail_closed(store,monkeypatch):
    monkeypatch.setenv("NEXORA_INTELLIGENCE_LEARNING_PROMOTION_ENABLED","true")
    source=_market_knowledge()
    stage,_=_staged_validated(market_source=source)
    with store() as session:
        row=session.get(HistoricalResearchReport,stage["artifact_id"])
        payload=json.loads(row.payload_json)
        payload["report"]["holdout_accuracy"]=.3
        row.payload_json=json.dumps(payload,sort_keys=True)
        session.commit()
    with pytest.raises(kb.KnowledgeUnavailable):
        reg.validate_research_challenger(
            model_knowledge_id=stage["knowledge_id"],
            model_digest=stage["report"]["model_id_sha256"],
            expected_revision=6,now=NOW)


def test_event_queue_dedup_and_existing_cycle_without_second_worker(store,monkeypatch):
    source=_market_knowledge()
    fetch=AsyncMock(return_value=_history())
    monkeypatch.setattr(cycle,"collect_public_history",fetch)
    first=asyncio.run(cycle.run_scheduled_cycle(now=NOW))
    assert first["state"]=="CHALLENGER_STAGED_AWAITING_APPROVAL"
    assert first["execution_allowed"] is False
    assert asyncio.run(cycle.run_scheduled_cycle(now=NOW))["state"]=="NOT_DUE"
    ev1=cycle.record_market_event(
        market=MARKET,symbol=SYMBOL,timeframe_seconds=TF,
        observed_regime="UPTREND",now=NOW+timedelta(minutes=1))
    ev2=cycle.record_market_event(
        market=MARKET,symbol=SYMBOL,timeframe_seconds=TF,
        observed_regime="UPTREND",now=NOW+timedelta(minutes=2))
    assert ev1["state"]=="QUEUED"
    assert ev2["state"]=="DEDUPLICATED"
    second=asyncio.run(cycle.run_scheduled_cycle(now=NOW+timedelta(minutes=3)))
    assert second["event_id"]==ev1["event_id"]
    assert second["state"]=="CHALLENGER_STAGED_AWAITING_APPROVAL"
    assert fetch.await_count==2
    with store() as session:
        assert session.query(HistoricalResearchReport).filter(
            HistoricalResearchReport.research_key==cycle.CYCLE_KEY
        ).count()==2


def test_market_change_event_does_not_retrain_invalidated_knowledge(store):
    source=_market_knowledge(regime="UPTREND")
    changed=kb.invalidate_on_market_change(
        market=MARKET,symbol=SYMBOL,observed_regime="DOWNTREND",
        now=NOW+timedelta(hours=1))
    assert changed==1
    with store() as session:
        rows=session.scalars(select(HistoricalResearchReport).where(
            HistoricalResearchReport.research_key.like("I05_EVT_%"))).all()
        assert len(rows)==1
    audit=asyncio.run(cycle.run_scheduled_cycle(
        now=NOW+timedelta(hours=1,minutes=3)))
    assert audit["state"]=="SKIPPED_NO_VALIDATED_MARKET_SOURCE"
    assert audit["event_id"]==rows[0].id


def test_learning_artifact_retention_does_not_destroy_audit_chain(store):
    with store() as session:
        for i in range(13):
            session.add(HistoricalResearchReport(
                research_key="I05_REL_STATIC_TEST",created_at=NOW-timedelta(days=20+i),
                period_from=NOW-timedelta(days=30),
                period_to=NOW-timedelta(days=20),
                payload_json='{"research_only":true}',
            ))
        session.commit()
    cleanup_research_reports(now=NOW,session_factory=store)
    with store() as session:
        assert session.query(HistoricalResearchReport).filter(
            HistoricalResearchReport.research_key=="I05_REL_STATIC_TEST"
        ).count()==13



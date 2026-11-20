"""I04 shared knowledge base: synthetic data only, no Deriv or production DB."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

from app import db, main, real_broker
from app.intelligence2 import knowledge_base as kb
from app.research_persistence import HistoricalResearchReport
from app.security import encrypt, hashsecret

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/i04.sqlite", pool_pre_ping=True)
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    yield factory
    engine.dispose()


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _discover(subject="regime_observation", **kwargs):
    return kb.discover(
        category=kwargs.get("category", "MARKET"),
        market=kwargs.get("market", "forex"),
        symbol=kwargs.get("symbol", "frxEURUSD"),
        timeframe_seconds=kwargs.get("timeframe_seconds", 900),
        subject=subject, model_version=kwargs.get("model_version", "i03_v1"),
        regime=kwargs.get("regime", "UPTREND"),
        attributes=kwargs.get("attributes", {
            "regime":"UPTREND", "data_quality":"COMPLETE",
            "sample_size":144, "confidence_heuristic":.78,
        }),
        valid_from=kwargs.get("valid_from", NOW - timedelta(days=2)),
        valid_until=kwargs.get("valid_until", NOW + timedelta(days=7)),
        next_review_at=kwargs.get("next_review_at", NOW + timedelta(days=1)),
        now=kwargs.get("now", NOW),
    )


def _evidence(item, tag, *, source="PUBLIC_CONTEXT", observations=144,
              verified=True, group_size=0, now=NOW, **kwargs):
    return kb.append_evidence(
        knowledge_id=item["id"], expected_revision=item["revision"],
        source_type=source, source_ref=_sha(tag + "_ref"),
        source_digest=_sha(tag + "_digest"),
        origin_group=kwargs.get("origin_group", tag.upper()),
        observation_count=observations, period_from=now-timedelta(days=1),
        period_to=now-timedelta(hours=1),
        verified=verified, group_size=group_size,
        summary={"sample_size": observations, "data_quality":"COMPLETE"},
        now=now,
    )


def _active():
    row = _discover()
    row = kb.change_state(knowledge_id=row["id"],
                          expected_revision=row["revision"],
                          target_state="UNDER_VALIDATION",
                          reason_code="START_PUBLIC_REVIEW", now=NOW)
    row = _evidence(row,"feed_one")
    row = _evidence(row,"feed_two")
    row = kb.change_state(knowledge_id=row["id"],
                          expected_revision=row["revision"],
                          target_state="VALIDATED",
                          reason_code="TWO_PUBLIC_SOURCES", now=NOW)
    return kb.change_state(knowledge_id=row["id"],
                           expected_revision=row["revision"],
                           target_state="ACTIVE",
                           reason_code="APPROVED_FOR_RESEARCH", now=NOW)


def test_discovery_is_persistent_public_idempotent_not_active(storage):
    first = _discover()
    duplicate = _discover()
    assert first["id"] == duplicate["id"] and first["revision"] == 1
    assert first["state"] == "DISCOVERED"
    assert first["reusable_for_research"] is False
    assert first["execution_allowed"] is False
    with storage() as session:
        assert session.query(db.IntelligenceKnowledge).count() == 1
        assert session.query(db.IntelligenceKnowledgeDecision).count() == 1
        assert not any(col.name in ("user_id","account_id","balance","stake","pnl")
                       for col in db.IntelligenceKnowledge.__table__.columns)


def test_public_evidence_two_independent_sources_required_for_activation(storage):
    item = _discover()
    with pytest.raises(kb.KnowledgeError):
        kb.change_state(knowledge_id=item["id"],expected_revision=item["revision"],
                        target_state="ACTIVE", reason_code="BYPASS",now=NOW)
    item = kb.change_state(knowledge_id=item["id"],expected_revision=item["revision"],
                           target_state="UNDER_VALIDATION",reason_code="REVIEW_STARTED",now=NOW)
    item = _evidence(item,"alpha")
    with pytest.raises(kb.KnowledgeError,match="insuficientes"):
        kb.change_state(knowledge_id=item["id"], expected_revision=item["revision"],
                        target_state="VALIDATED",reason_code="ONE_SOURCE",now=NOW)
    item = _evidence(item,"beta",verified=False)
    with pytest.raises(kb.KnowledgeError,match="insuficientes"):
        kb.change_state(knowledge_id=item["id"], expected_revision=item["revision"],
                        target_state="VALIDATED",reason_code="UNVERIFIED",now=NOW)
    item = _evidence(item,"gamma",verified=True,origin_group="ALPHA")
    with pytest.raises(kb.KnowledgeError,match="insuficientes"):
        kb.change_state(knowledge_id=item["id"], expected_revision=item["revision"],
                        target_state="VALIDATED",reason_code="DUPLICATE_GROUP",now=NOW)
    item = _evidence(item,"delta",verified=True,origin_group="DIFFERENT_FEED")
    item = kb.change_state(knowledge_id=item["id"],expected_revision=item["revision"],
                           target_state="VALIDATED",reason_code="TWO_PUBLIC_SOURCES",now=NOW)
    item = kb.change_state(knowledge_id=item["id"],expected_revision=item["revision"],
                           target_state="ACTIVE",reason_code="RESEARCH_ONLY",now=NOW)
    assert item["state"] == "ACTIVE"
    assert item["reusable_for_research"] is False  # Current regime not supplied.
    view = kb.get_knowledge(item["id"],now=NOW,observed_regime="UPTREND")
    assert view["reusable_for_research"] is True
    assert len(view["evidence"]) == 4
    assert len(view["decisions"]) == item["revision"]
    assert view["decisions"][-1]["reason_code"] == "RESEARCH_ONLY"
    assert "public_evidence_summary" in view["evidence"][0]
    assert all("account" not in str(x) for x in view["evidence"])


def test_retrieval_is_shared_current_and_conservative(storage):
    item = _active()
    good = kb.retrieve_active(category="MARKET",market="forex",
                              symbol="frxEURUSD",observed_regime="UPTREND",now=NOW)
    assert [x["id"] for x in good["active"]] == [item["id"]]
    assert good["read_only"] and not good["execution_allowed"]
    missing_regime = kb.retrieve_active(category="MARKET",market="forex",
                                        symbol="frxEURUSD",now=NOW)
    assert missing_regime["active"] == []
    assert missing_regime["excluded"][0]["reason"] == "REGIME_CONTEXT_REQUIRED"
    mismatch = kb.retrieve_active(category="MARKET",market="forex",
                                  symbol="frxEURUSD",observed_regime="DOWNTREND",now=NOW)
    assert mismatch["active"] == []
    assert mismatch["excluded"][0]["reason"] == "REGIME_CHANGED"
    overdue = kb.retrieve_active(category="MARKET",market="forex",
                                  symbol="frxEURUSD",observed_regime="UPTREND",
                                  now=NOW+timedelta(days=2))
    assert overdue["active"] == []
    assert overdue["excluded"][0]["reason"] == "PERIODIC_REVIEW_DUE"
    expired = kb.retrieve_active(category="MARKET",market="forex",
                                  symbol="frxEURUSD",observed_regime="UPTREND",
                                  now=NOW+timedelta(days=8))
    assert expired["active"] == []
    assert expired["excluded"][0]["reason"] == "VALIDITY_EXPIRED"


def test_versioned_cas_rejects_stale_writes_and_conflicting_identity(storage):
    item = _discover()
    with pytest.raises(kb.KnowledgeConflict):
        _discover(attributes={"regime":"DOWNTREND"})
    updated = kb.change_state(knowledge_id=item["id"],expected_revision=1,
                              target_state="UNDER_VALIDATION",
                              reason_code="START_VALIDATION", now=NOW)
    assert updated["revision"] == 2
    with pytest.raises(kb.KnowledgeConflict,match="CAS"):
        kb.change_state(knowledge_id=item["id"],expected_revision=1,
                        target_state="INVALIDATED",reason_code="STALE_WRITER",now=NOW)
    with storage() as s:
        decisions=s.scalars(select(db.IntelligenceKnowledgeDecision).where(
            db.IntelligenceKnowledgeDecision.knowledge_id==item["id"]).order_by(
                db.IntelligenceKnowledgeDecision.revision)).all()
        assert [d.revision for d in decisions] == [1,2]


def test_evidence_dedupe_preserves_revision_and_source_conflicts(storage):
    item = _discover()
    item = _evidence(item,"source_one")
    repeated = _evidence(item,"source_one")
    assert repeated["revision"] == item["revision"]
    with pytest.raises(kb.KnowledgeConflict):
        kb.append_evidence(
            knowledge_id=item["id"],expected_revision=item["revision"],
            source_type="PUBLIC_CONTEXT",source_ref=_sha("source_one_ref"),
            source_digest=_sha("different"),origin_group="NEW_SOURCE",
            observation_count=99,period_from=NOW-timedelta(days=1),
            period_to=NOW,verified=True,
            summary={"sample_size":99},now=NOW)


@pytest.mark.parametrize("data", [
    {"user_id":123}, {"balance":100}, {"account_id":"CR00000"},
    {"stake":1}, {"pnl":5}, {"token":"xyz"},
    {"aggregate_size":1}, {"confidence_heuristic":2.0},
    {"regime":"my secret value with spaces"},
    {"sample_size":float("nan")}, {"error_rate":-1},
])
def test_private_or_invalid_payload_rejected(storage, data):
    with pytest.raises(kb.KnowledgeError):
        _discover(attributes=data)


def test_aggregate_execution_requires_group_anonymity_and_evidence(storage):
    item = _discover("public_execution_quality", category="EXECUTION",
                     attributes={"aggregate_size":30,"latency_ms_p95":420.0})
    item = kb.change_state(knowledge_id=item["id"],expected_revision=item["revision"],
                           target_state="UNDER_VALIDATION",
                           reason_code="START_REVIEW",now=NOW)
    with pytest.raises(kb.KnowledgeError,match="agregado"):
        _evidence(item,"agg_invalid",source="AGGREGATED_EXECUTION",
                  observations=100, group_size=3)
    item = _evidence(item,"agg_a",source="AGGREGATED_EXECUTION",
                     observations=100,group_size=30)
    item = _evidence(item,"agg_b",source="AGGREGATED_EXECUTION",
                     observations=100,group_size=32)
    item = kb.change_state(knowledge_id=item["id"],expected_revision=item["revision"],
                           target_state="VALIDATED",reason_code="GROUPS_VERIFIED",now=NOW)
    assert item["state"] == "VALIDATED"
    with pytest.raises(kb.KnowledgeError):
        _discover("private_execution", category="EXECUTION",
                  attributes={"latency_ms_p95":42})


def test_models_never_promoted_or_trained_in_i04(storage):
    item = _discover("research_model",category="MODEL",
                     attributes={"model_id":"untrained_candidate"})
    item = kb.change_state(knowledge_id=item["id"], expected_revision=1,
                           target_state="UNDER_VALIDATION",reason_code="MODEL_DISCOVERY",now=NOW)
    item = _evidence(item,"mod_a",source="MODEL_ARTIFACT")
    item = _evidence(item,"mod_b",source="MODEL_ARTIFACT")
    with pytest.raises(kb.KnowledgeError,match="não promove modelos"):
        kb.change_state(knowledge_id=item["id"],expected_revision=item["revision"],
                        target_state="VALIDATED",reason_code="MODEL_NOT_APPROVED",now=NOW)
    assert kb.get_knowledge(item["id"],now=NOW)["state"] == "UNDER_VALIDATION"


def test_revalidation_requires_fresh_evidence_and_explicit_renewal(storage):
    item = _active()
    after = NOW+timedelta(days=3)
    item = kb.change_state(knowledge_id=item["id"],expected_revision=item["revision"],
                           target_state="REVIEW_REQUIRED",reason_code="REVIEW_TRIGGER",now=after)
    item = kb.change_state(knowledge_id=item["id"],expected_revision=item["revision"],
                           target_state="INVALIDATED",reason_code="MARKET_SHIFT",now=after)
    with pytest.raises(kb.KnowledgeError,match="vencido"):
        kb.change_state(knowledge_id=item["id"],expected_revision=item["revision"],
                        target_state="REVALIDATED",reason_code="RETRY_WITHOUT_RENEWAL",now=after)
    item = kb.revise_validity(
        knowledge_id=item["id"],expected_revision=item["revision"],
        valid_from=after-timedelta(hours=1),valid_until=after+timedelta(days=14),
        next_review_at=after+timedelta(days=2),reason_code="NEW_REVIEW_WINDOW",now=after)
    with pytest.raises(kb.KnowledgeError,match="insuficientes"):
        kb.change_state(knowledge_id=item["id"],expected_revision=item["revision"],
                        target_state="REVALIDATED",reason_code="NO_NEW_EVIDENCE",now=after)
    item = _evidence(item,"fresh_a",now=after)
    item = _evidence(item,"fresh_b",now=after)
    item = kb.change_state(knowledge_id=item["id"],expected_revision=item["revision"],
                           target_state="REVALIDATED",reason_code="NEW_EVIDENCE",now=after)
    item = kb.change_state(knowledge_id=item["id"],expected_revision=item["revision"],
                           target_state="ACTIVE",reason_code="REACTIVATED_PUBLIC",now=after)
    assert item["state"] == "ACTIVE"
    good = kb.retrieve_active(category="MARKET",market="forex",
                              symbol="frxEURUSD",observed_regime="UPTREND",now=after)
    assert len(good["active"]) == 1


def test_periodic_review_reuses_existing_maintenance_and_never_promotes(storage,monkeypatch):
    item = _active()
    assert kb.review_due_knowledge(now=NOW+timedelta(days=2)) == 0
    monkeypatch.setenv("NEXORA_INTELLIGENCE_KNOWLEDGE_ENABLED","true")
    assert kb.review_due_knowledge(now=NOW+timedelta(days=2)) == 1
    assert kb.review_due_knowledge(now=NOW+timedelta(days=2)) == 0
    view = kb.get_knowledge(item["id"],now=NOW+timedelta(days=2))
    assert view["state"] == "REVIEW_REQUIRED"
    assert view["reusable_for_research"] is False


def test_market_regime_change_persists_invalidation_idempotently(storage):
    item = _active()
    assert kb.invalidate_on_market_change(market="forex",symbol="frxEURUSD",
                                          observed_regime="UPTREND",now=NOW) == 0
    assert kb.invalidate_on_market_change(market="forex",symbol="frxEURUSD",
                                          observed_regime="DOWNTREND",now=NOW) == 1
    assert kb.invalidate_on_market_change(market="forex",symbol="frxEURUSD",
                                          observed_regime="DOWNTREND",now=NOW) == 0
    result = kb.retrieve_active(category="MARKET",market="forex",
                                symbol="frxEURUSD",observed_regime="UPTREND",now=NOW)
    assert result["active"] == []
    assert result["excluded"][0]["state"] == "INVALIDATED"


def test_integrity_tamper_on_payload_and_lineage_fails_closed(storage):
    item = _active()
    with storage() as session:
        session.execute(update(db.IntelligenceKnowledge).where(
            db.IntelligenceKnowledge.id == item["id"]).values(
                payload_json='{"regime":"DOWNTREND"}'
            ))
        session.commit()
    with pytest.raises(kb.KnowledgeUnavailable,match="Assinatura"):
        kb.get_knowledge(item["id"],now=NOW)
    with storage() as session:
        row=session.get(db.IntelligenceKnowledge,item["id"])
        row.payload_json='{"regime":"UPTREND","data_quality":"COMPLETE","sample_size":144,"confidence_heuristic":0.78}'
        session.commit()
    with pytest.raises(kb.KnowledgeUnavailable):
        kb.retrieve_active(category="MARKET",market="forex",symbol="frxEURUSD",now=NOW)


def test_existing_research_report_used_as_verified_evidence(storage):
    with storage() as session:
        report = HistoricalResearchReport(
            research_key="1HZ100V_M1_M15_30D_V1",created_at=NOW,
            period_from=NOW-timedelta(days=20),period_to=NOW-timedelta(days=1),
            payload_json='{"results":[{"holdout":{"signals":120}},{"holdout":{"signals":60}}]}')
        session.add(report)
        session.commit()
        rid=report.id
    item = _discover("strategy_hypothesis",category="STRATEGY",
                     attributes={"strategy_family":"ema_trend",
                                 "holdout_samples":120})
    item = kb.change_state(knowledge_id=item["id"],expected_revision=item["revision"],
                           target_state="UNDER_VALIDATION",reason_code="HOLDOUT_REVIEW",now=NOW)
    item = _evidence(item,"i03",source="PUBLIC_CONTEXT")
    with pytest.raises(kb.KnowledgeError,match="insuficientes"):
        kb.change_state(knowledge_id=item["id"],expected_revision=item["revision"],
                        target_state="VALIDATED",reason_code="NO_HOLDOUT",now=NOW)
    item = kb.append_research_report_evidence(
        knowledge_id=item["id"],expected_revision=item["revision"],
        report_id=rid,now=NOW)
    view=kb.get_knowledge(item["id"],now=NOW)
    assert view["evidence"][-1]["public_evidence_summary"]["holdout_samples"] == 120
    item = kb.change_state(knowledge_id=item["id"],expected_revision=item["revision"],
                           target_state="VALIDATED",reason_code="HOLDOUT_WITH_CONTEXT",now=NOW)
    assert item["state"] == "VALIDATED"
    with pytest.raises(kb.KnowledgeUnavailable):
        kb.append_research_report_evidence(
            knowledge_id=item["id"],expected_revision=item["revision"],
            report_id=rid+100,now=NOW)


def test_authenticated_mode_independent_read_only_api_and_disabled_guard(
    storage,monkeypatch,
):
    item=_active()
    with storage() as session:
        for account, sid, mode in (
            ("V_KB_DEMO","kb-demo","demo"),("CR_KB_REAL","kb-real","real")
        ):
            user=db.User(deriv_account_id=account,selected_mode=mode)
            session.add(user);session.flush()
            session.add(db.Session(
                session_hash=hashsecret(sid),user_id=user.id,
                token_encrypted=encrypt("synthetic-local-only"),
                expires_at=db.utcnow()+timedelta(hours=1)))
        session.commit()
    client=TestClient(main.app)
    forbidden=AsyncMock(side_effect=AssertionError("No trading orders in I04"))
    monkeypatch.setattr(real_broker,"buy_real_contract",forbidden)
    try:
        assert client.get("/api/research/knowledge?category=MARKET&market=forex").status_code==401
        client.cookies.set("nexora_sid","kb-demo")
        disabled=client.get("/api/research/knowledge?category=MARKET&market=forex")
        assert disabled.status_code==503
        monkeypatch.setenv("NEXORA_INTELLIGENCE_KNOWLEDGE_ENABLED","true")
        demo=client.get(
            "/api/research/knowledge?category=MARKET&market=forex&symbol=frxEURUSD&observed_regime=UPTREND"
        )
        assert demo.status_code==200
        client.cookies.set("nexora_sid","kb-real")
        real=client.get(
            "/api/research/knowledge?category=MARKET&market=forex&symbol=frxEURUSD&observed_regime=UPTREND"
        )
        assert real.status_code==200
        assert demo.json()["active"] == real.json()["active"]
        assert all("user_id" not in str(row) for row in demo.json()["active"])
        assert client.get(f"/api/research/knowledge/{item['id']}?observed_regime=UPTREND").status_code==200
        assert client.get("/api/research/knowledge?category=BAD&market=forex").status_code==422
        forbidden.assert_not_awaited()
    finally:
        client.close()

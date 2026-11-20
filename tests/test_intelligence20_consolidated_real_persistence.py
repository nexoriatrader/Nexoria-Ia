"""Real I04 + I05 persistence in disposable SQLite/Postgres, no production data.

These are integration tests against the actual PR #54/#56 modules and the
existing PR #60/#61/#62 public adapters; NOT synthetic model provider mocks.
No broker, orders, finance, DEMO/REAL access or runtime schema migration.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from app import db
from app.research_persistence import HistoricalResearchReport
from app.intelligence2 import knowledge_base as kb
from app.intelligence2 import learning_engine as learning
from app.intelligence2 import learning_registry as registry
from app.intelligence2 import i04_research_adapter as i04_adapter
from app.intelligence2 import i05_research_adapter as i05_adapter
from app.intelligence2 import research_orchestrator

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
NOW_EPOCH = int(NOW.timestamp())
MARKET = "derived"
SYMBOL = "R_100"
TF = 60


@pytest.fixture
def installed_test_schema(tmp_path, monkeypatch):
    url = os.getenv("NEXORA_CONSOLIDATION_TEST_DATABASE_URL") or (
        "sqlite:///" + str(tmp_path / "integration.sqlite")
    )
    # All DDL here is explicit TEST-ONLY creation on a disposable database;
    # app.db.init_db() does NOT own knowledge tables.
    engine = create_engine(url, pool_pre_ping=True)
    HistoricalResearchReport.__table__.create(engine, checkfirst=True)
    db.KnowledgeMetadata.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setenv("NEXORA_INTELLIGENCE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("NEXORA_INTELLIGENCE_LEARNING_ENABLED", "true")
    monkeypatch.setenv("NEXORA_INTELLIGENCE_LEARNING_PROMOTION_ENABLED", "false")
    try:
        yield factory
    finally:
        db.KnowledgeMetadata.metadata.drop_all(engine)
        HistoricalResearchReport.__table__.drop(engine, checkfirst=True)
        engine.dispose()


def _active_market():
    item = kb.discover(
        category="MARKET", market=MARKET, symbol=SYMBOL,
        timeframe_seconds=TF, subject="public_regime_observation",
        model_version="i03_context_v1", regime="UNKNOWN",
        attributes={
            "regime": "UPTREND", "data_quality": "COMPLETE",
            "sample_size": 1000,
        },
        valid_from=NOW-timedelta(days=2),
        valid_until=NOW+timedelta(days=30),
        next_review_at=NOW+timedelta(days=7), now=NOW,
    )
    item = kb.change_state(
        knowledge_id=item["id"], expected_revision=item["revision"],
        target_state="UNDER_VALIDATION", reason_code="BEGIN_PUBLIC_REVIEW",
        now=NOW,
    )
    for idx, group in enumerate(("INDEPENDENT_ALPHA", "INDEPENDENT_BETA")):
        item = kb.append_evidence(
            knowledge_id=item["id"], expected_revision=item["revision"],
            source_type="PUBLIC_CONTEXT",
            source_ref=("%064x" % (idx+100)),
            source_digest=("%064x" % (idx+200)),
            origin_group=group, observation_count=144,
            period_from=NOW-timedelta(days=1),
            period_to=NOW-timedelta(hours=1),
            verified=True,
            summary={"sample_size": 144, "data_quality": "COMPLETE"},
            now=NOW,
        )
    item = kb.change_state(
        knowledge_id=item["id"], expected_revision=item["revision"],
        target_state="VALIDATED", reason_code="TWO_PUBLIC_ORIGINS",
        now=NOW,
    )
    return kb.change_state(
        knowledge_id=item["id"], expected_revision=item["revision"],
        target_state="ACTIVE", reason_code="PUBLIC_RESEARCH_APPROVED",
        now=NOW,
    )


def _base_orchestrated():
    return {
        "version": research_orchestrator.VERSION,
        "as_of_epoch": NOW_EPOCH,
        "state": "RESEARCH_CANDIDATES",
        "assets": [{
            "market": MARKET, "symbol": SYMBOL,
            "state": "RESEARCH_READY",
            "observed_trend": "UP",
            "context_consensus": "THREE_TIMEFRAMES",
            "research_timeframes_seconds": {
                "higher": 3600, "intermediate": 300, "lower": TF,
            },
            "strategy_family": "public_regime_observation",
            "evidence_completeness_points": 100,
            "reason_codes": ["PUBLIC_DATA_READY_FOR_STUDY"],
            "execution_allowed": False,
            "approved_for_trading": False,
        }],
        "counts": {"RESEARCH_READY": 1, "WAIT_RECHECK": 0, "EXCLUDED": 0},
        "integrations": {"i04": "NOT_CONNECTED", "i05": "NOT_CONNECTED"},
        "distribution": {"publish_allowed": False, "execution_allowed": False},
        "orders_sent": 0,
        "execution_allowed": False,
        "approved_for_trading": False,
    }


def _history():
    end = (NOW_EPOCH // TF - 1) * TF
    size = learning.MIN_HISTORY - learning.MIN_BARS + 1
    labels = ("UPTREND", "DOWNTREND", "RANGING", "TRANSITION")
    return {
        "version": learning.SOURCE_VERSION,
        "market": MARKET, "symbol": SYMBOL, "timeframe_seconds": TF,
        "observed_at_epoch": NOW_EPOCH,
        "data_quality": "COMPLETE",
        "context_version": learning.CONTEXT_VERSION,
        "i01_last_closed_epoch": end,
        "observations": [
            {"closed_epoch": end-(size-1-i)*TF,
             "regime": labels[i % len(labels)]}
            for i in range(size)
        ],
        "execution_allowed": False,
    }


def test_knowledge_tables_are_same_engine_but_not_automatic_db_metadata():
    names = set(db.KnowledgeMetadata.metadata.tables)
    assert names == {
        "intelligence_knowledge",
        "intelligence_knowledge_evidence",
        "intelligence_knowledge_decisions",
    }
    assert names.isdisjoint(db.Base.metadata.tables)
    assert db.IntelligenceKnowledge.__table__.metadata is db.KnowledgeMetadata.metadata
    assert db.IntelligenceKnowledgeEvidence.__table__.metadata is db.KnowledgeMetadata.metadata


def test_real_i04_knowledge_persists_and_retrieves_after_new_session(
    installed_test_schema, monkeypatch,
):
    item = _active_market()
    assert item["state"] == "ACTIVE"
    duplicate = kb.discover(
        category="MARKET", market=MARKET, symbol=SYMBOL,
        timeframe_seconds=TF, subject="public_regime_observation",
        model_version="i03_context_v1", regime="UNKNOWN",
        attributes={"regime": "UPTREND", "data_quality": "COMPLETE",
                    "sample_size": 1000},
        valid_from=NOW-timedelta(days=2),
        valid_until=NOW+timedelta(days=30),
        next_review_at=NOW+timedelta(days=7), now=NOW,
    )
    assert duplicate["id"] == item["id"]
    with installed_test_schema() as session:
        assert session.scalar(select(db.IntelligenceKnowledge.id)) == item["id"]
        assert session.query(db.IntelligenceKnowledgeEvidence).count() == 2
        assert session.query(db.IntelligenceKnowledgeDecision).count() == item["revision"]
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(
        bind=installed_test_schema.kw["bind"], expire_on_commit=False
    ))
    read = kb.retrieve_active(
        category="MARKET", market=MARKET, symbol=SYMBOL,
        observed_regime="UPTREND", now=NOW, limit=8,
    )
    assert [x["id"] for x in read["active"]] == [item["id"]]
    assert kb.get_knowledge(item["id"], now=NOW,
                            observed_regime="UPTREND")["reusable_for_research"] is True


def test_real_i04_orchestrator_adapter_uses_persisted_knowledge(
    installed_test_schema,
):
    source = _active_market()
    result = i04_adapter.enrich_research_with_i04(
        _base_orchestrated(), now=NOW_EPOCH, provider=kb,
    )
    kb_view = result["assets"][0]["knowledge_base"]
    assert kb_view["status"] == "I04_CURRENT_ACTIVE_VERIFIED"
    assert kb_view["used_count"] == 1
    assert kb_view["used_public_knowledge"][0]["knowledge_id"] == source["id"]
    assert kb_view["used_public_knowledge"][0]["revision"] == source["revision"]
    assert result["assets"][0]["state"] == "RESEARCH_READY"
    assert result["distribution"]["publish_allowed"] is False
    assert result["orders_sent"] == 0


def test_real_i04_expired_review_cannot_contribute(installed_test_schema):
    _active_market()
    result = i04_adapter.enrich_research_with_i04(
        _base_orchestrated(), now=NOW_EPOCH + 8*86400, provider=kb,
    )
    assert result["assets"][0]["knowledge_base"]["used_count"] == 0
    assert result["assets"][0]["knowledge_base"]["status"] == "I04_NO_REUSABLE_KNOWLEDGE"


def test_real_i05_no_release_means_no_model_even_with_public_i04(
    installed_test_schema,
):
    _active_market()
    source = i04_adapter.enrich_research_with_i04(
        _base_orchestrated(), now=NOW_EPOCH, provider=kb,
    )
    result = i05_adapter.enrich_research_with_i05(
        source, now=NOW_EPOCH, registry=registry, knowledge=kb,
    )
    learning_view = result["assets"][0]["learning_engine"]
    assert learning_view["status"] == "I05_NO_VALIDATED_RESEARCH_MODEL"
    assert learning_view["champion"] is None
    assert learning_view["automatic_promotion_performed"] is False
    assert result["integrations"]["i05"] == "READ_ONLY_CONNECTED"
    assert result["assets"][0]["evidence_completeness_points"] == 100


def test_real_i05_stage_public_model_only_persists_challenger_without_promotion(
    installed_test_schema,
):
    market = _active_market()
    evaluated = learning.evaluate_public_regimes(_history())
    assert evaluated["research_eligible"] is True
    assert evaluated["model_version"].startswith("i05_")
    candidate = registry.stage_public_candidate(
        _history(), market_knowledge_id=market["id"], now=NOW,
    )
    assert candidate["state"] == "UNDER_VALIDATION"
    assert candidate["report"]["model_id_sha256"] == evaluated["model_id_sha256"]
    assert candidate["execution_allowed"] is False
    with installed_test_schema() as session:
        assert session.get(db.IntelligenceKnowledge, candidate["knowledge_id"]).state == "UNDER_VALIDATION"
        assert session.get(HistoricalResearchReport, candidate["artifact_id"]) is not None
    # A persisted challenger alone cannot become a research Champion.
    release = registry.get_research_release(
        market=MARKET, symbol=SYMBOL, timeframe_seconds=TF, now=NOW,
    )
    assert release["state"] == "NO_VALIDATED_RESEARCH_MODEL"
    assert release["champion_knowledge_id"] is None


def test_i05_historical_releases_not_pruned_by_regular_retention(
    installed_test_schema,
):
    from app.research_retention import cleanup_research_reports
    ancient = NOW - timedelta(days=40)
    with installed_test_schema() as session:
        for ix in range(15):
            session.add(HistoricalResearchReport(
                research_key="I05_REL_RESEARCH_HISTORY",
                period_from=ancient, period_to=ancient,
                created_at=ancient,
                payload_json='{"version":"research-only"}',
            ))
        session.commit()
    cleanup_research_reports(now=NOW, session_factory=installed_test_schema)
    with installed_test_schema() as session:
        remaining = session.query(HistoricalResearchReport).filter_by(
            research_key="I05_REL_RESEARCH_HISTORY"
        ).count()
        assert remaining == 15

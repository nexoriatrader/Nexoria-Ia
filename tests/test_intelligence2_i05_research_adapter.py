"""I05 registry and I04 model lineage read-only contract integration.

Synthetic public knowledge and release objects use the existing I05 PR #56
method signatures and I04 get_knowledge payload. No model training, promotion,
account data, broker calls, migrations, financial results or new storage.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.intelligence2 import i04_research_adapter as i04a
from app.intelligence2 import i05_research_adapter as i05a
from app.intelligence2 import research_orchestrator

NOW = 50_000_000
TF = 60
MODEL_SHA = "a" * 64


def _date(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _research():
    return {
        "version": research_orchestrator.VERSION,
        "as_of_epoch": NOW,
        "state": "RESEARCH_CANDIDATES",
        "assets": [{
            "symbol": "FRXEURUSD",
            "market": "forex",
            "state": "RESEARCH_READY",
            "observed_trend": "UP",
            "context_consensus": "THREE_TIMEFRAMES",
            "research_timeframes_seconds": {
                "higher": 3600,
                "intermediate": 300,
                "lower": TF,
            },
            "strategy_family": "public_forex_observation",
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


def _model(model_id=101):
    evidence = [
        {
            "source_type": "MODEL_ARTIFACT",
            "origin_group": group,
            "observations": size,
            "verified": True,
            "period_from": _date(start),
            "period_to": _date(end),
            "source_sha256": character * 64,
        }
        for group, size, start, end, character in (
            ("PUBLIC_TRAIN_WINDOW", 600, NOW-90_000, NOW-60_000, "c"),
            ("PUBLIC_HOLDOUT_WINDOW", 240, NOW-55_000, NOW-30_000, "d"),
            ("PUBLIC_INDEPENDENT_VALIDATION", 240, NOW-25_000, NOW-5_000, "e"),
        )
    ]
    return {
        "version": i05a.I04_VERSION,
        "id": model_id,
        "revision": 7,
        "category": "MODEL",
        "market": "forex", "symbol": "FRXEURUSD", "timeframe_seconds": TF,
        "model_version": "REGIME_MARKOV_LAPLACE_V1",
        "regime": "UNKNOWN",
        "state": "ACTIVE",
        "reusable_for_research": True,
        "reuse_status": "CURRENT_VERIFIED_ACTIVE",
        "execution_allowed": False, "approved_for_trading": False,
        "valid_from": _date(NOW - 1_000),
        "valid_until": _date(NOW + 36_000),
        "next_review_at": _date(NOW + 14_400),
        "payload_sha256": "b" * 64,
        "payload": {
            "model_id": ("a" if model_id == 101 else "f") * 64,
            "source_version": i05a.I05_SOURCE_VERSION,
            "sample_size": 600, "holdout_samples": 240,
            "success_rate": .62,
        },
        "evidence": evidence,
        "decisions": [
            {"revision": 1, "to_state": "DISCOVERED",
             "decision": "DISCOVERY"},
            {"revision": 7, "to_state": "ACTIVE",
             "decision": "I05_RESEARCH_ONLY_PROMOTION"},
        ],
    }


def _release(**changes):
    return {
        "version": i05a.I05_VERSION,
        "state": "RESEARCH_CHAMPION",
        "release_id": 55,
        "generation": 3,
        "champion_knowledge_id": 101,
        "fallback_knowledge_id": 102,
        "release_event": "MONITORED",
        "execution_allowed": False,
        "approved_for_trading": False,
        **changes,
    }


class PublicKnowledge:
    VERSION = i04a.I04_VERSION

    def __init__(self, *, models=None, enabled=True):
        self.models = models if models is not None else {
            101: _model(101), 102: _model(102)
        }
        self.enabled_flag = enabled
        self.model_reads = []
        self.context_queries = []

    def enabled(self):
        return self.enabled_flag

    def get_knowledge(self, knowledge_id, *, now, observed_regime,
                      include_lineage):
        self.model_reads.append(knowledge_id)
        assert now == datetime.fromtimestamp(NOW, tz=timezone.utc)
        assert observed_regime == "UPTREND"
        assert include_lineage is True
        if knowledge_id not in self.models:
            raise ValueError("I04 missing")
        return deepcopy(self.models[knowledge_id])

    def retrieve_active(self, *, category, market, symbol, observed_regime,
                        now, limit):
        self.context_queries.append(category)
        assert category in ("MARKET", "STRATEGY")
        assert observed_regime == "UPTREND"
        return {
            "version": self.VERSION,
            "category": category,
            "market": market,
            "symbol": symbol,
            "observed_regime": observed_regime,
            "active": [],
            "excluded": [],
            "read_only": True,
            "execution_allowed": False,
        }


class LearningRegistry:
    VERSION = i05a.I05_VERSION

    def __init__(self, *, release=None, enabled=True):
        self.release = _release() if release is None else release
        self.enabled_flag = enabled
        self.queries = []
        self.promotions = 0
        self.monitor_runs = 0

    def learning_enabled(self):
        return self.enabled_flag

    def get_research_release(self, *, market, symbol, timeframe_seconds, now):
        self.queries.append((market, symbol, timeframe_seconds, now))
        assert market == "forex"
        assert symbol == "FRXEURUSD"
        assert timeframe_seconds == TF
        assert now == datetime.fromtimestamp(NOW, tz=timezone.utc)
        return deepcopy(self.release)

    def promote_research_champion(self, **kwargs):
        self.promotions += 1
        raise AssertionError("promotion must never be called")

    def monitor_and_rollback(self, **kwargs):
        self.monitor_runs += 1
        raise AssertionError("monitor/rollback must never be called")


def _public_i04(report=None, *, provider=None):
    return i04a.enrich_research_with_i04(
        report if report is not None else _research(), now=NOW,
        provider=provider if provider is not None else PublicKnowledge(),
    )


def _enrich(*, registry=None, knowledge=None, source=None):
    knowledge = knowledge or PublicKnowledge()
    return i05a.enrich_research_with_i05(
        source or _public_i04(provider=knowledge), now=NOW,
        registry=registry or LearningRegistry(), knowledge=knowledge,
    )


def test_existing_i05_champion_fallback_and_oos_provenance_are_read_only():
    provider, registry = PublicKnowledge(), LearningRegistry()
    result = _enrich(knowledge=provider, registry=registry)
    asset = result["assets"][0]
    learning = asset["learning_engine"]
    assert result["state"] == "RESEARCH_CANDIDATES"
    assert asset["state"] == "RESEARCH_READY"
    assert result["integrations"]["i05"] == "READ_ONLY_CONNECTED"
    assert learning["status"] == "I05_CHAMPION_VERIFIED"
    assert learning["release_id"] == 55
    assert learning["release_generation"] == 3
    assert learning["release_event"] == "MONITORED"
    assert learning["champion"]["knowledge_id"] == 101
    assert learning["fallback"]["knowledge_id"] == 102
    assert learning["champion"]["model_version"] == "REGIME_MARKOV_LAPLACE_V1"
    assert learning["champion"]["regime_scope"] == "UNKNOWN"
    assert learning["champion"]["observed_public_regime"] == "UPTREND"
    periods = learning["champion"]["validation_periods"]
    assert set(periods) == {
        "PUBLIC_TRAIN_WINDOW", "PUBLIC_HOLDOUT_WINDOW",
        "PUBLIC_INDEPENDENT_VALIDATION"
    }
    assert periods["PUBLIC_INDEPENDENT_VALIDATION"]["observations"] == 240
    assert learning["model_contribution"] == "PUBLIC_MODEL_PROVENANCE_ONLY"
    assert learning["financial_source_used"] is False
    assert learning["automatic_promotion_performed"] is False
    assert learning["prediction_used_for_trading"] is False
    assert learning["challenger"]["status"] == "NOT_USED_UNTIL_SEPARATE_VALIDATION"
    assert learning["monitoring"]["monitor_or_rollback_invoked"] is False
    assert registry.promotions == registry.monitor_runs == 0
    assert len(registry.queries) == 1
    assert provider.model_reads == [101, 102]
    assert result["orders_sent"] == 0
    assert result["distribution"]["publish_allowed"] is False
    assert result["execution_allowed"] is False


def test_existing_i05_no_release_means_no_model_approval_or_influence():
    registry = LearningRegistry(release={
        "version": i05a.I05_VERSION,
        "state": "NO_VALIDATED_RESEARCH_MODEL",
        "champion_knowledge_id": None,
        "fallback_knowledge_id": None,
        "execution_allowed": False, "approved_for_trading": False,
    })
    provider = PublicKnowledge()
    result = _enrich(registry=registry, knowledge=provider)
    row = result["assets"][0]["learning_engine"]
    assert row["status"] == "I05_NO_VALIDATED_RESEARCH_MODEL"
    assert row["champion"] is None
    assert row["model_contribution"] == "NONE"
    assert provider.model_reads == []
    assert result["assets"][0]["state"] == "RESEARCH_READY"


@pytest.mark.parametrize("field,bad", [
    ("state", "VALIDATED_BUT_NOT_RELEASED"),
    ("version", "legacy_learning"),
    ("champion_knowledge_id", None),
    ("execution_allowed", True),
    ("approved_for_trading", True),
    ("user_id", 99),
])
def test_unverified_or_private_release_is_never_used(field, bad):
    provider, registry = PublicKnowledge(), LearningRegistry(
        release=_release(**{field: bad})
    )
    out = _enrich(registry=registry, knowledge=provider)
    row = out["assets"][0]["learning_engine"]
    assert row["status"] == "I05_RELEASE_OR_MODEL_UNAVAILABLE"
    assert row["model_contribution"] == "NONE"
    assert row["champion"] is None
    assert provider.model_reads == []


@pytest.mark.parametrize("mutation", [
    "inactive", "validated_only", "expired", "review_due",
    "scope_mismatch", "version_mismatch", "source_mismatch",
    "insufficient_training", "insufficient_holdout",
    "missing_second_holdout", "overlapping_holdouts",
    "unverified_holdout", "private_payload",
    "corrupt_decision", "missing_digest", "missing_model_id",
])
def test_unvalidated_stale_or_corrupted_champion_cannot_influence(mutation):
    provider = PublicKnowledge()
    model = provider.models[101]
    if mutation == "inactive":
        model["state"] = "INVALIDATED"
    elif mutation == "validated_only":
        model["state"] = "VALIDATED"
    elif mutation == "expired":
        model["valid_until"] = _date(NOW)
    elif mutation == "review_due":
        model["next_review_at"] = _date(NOW)
    elif mutation == "scope_mismatch":
        model["timeframe_seconds"] = 300
    elif mutation == "version_mismatch":
        model["model_version"] = "not valid version!"
    elif mutation == "source_mismatch":
        model["payload"]["source_version"] = "wrong"
    elif mutation == "insufficient_training":
        model["payload"]["sample_size"] = 100
    elif mutation == "insufficient_holdout":
        model["payload"]["holdout_samples"] = 50
    elif mutation == "missing_second_holdout":
        model["evidence"].pop()
    elif mutation == "overlapping_holdouts":
        model["evidence"][2]["period_from"] = _date(NOW - 32_000)
    elif mutation == "unverified_holdout":
        model["evidence"][2]["verified"] = False
    elif mutation == "private_payload":
        model["payload"]["account_id"] = "SECRET_FINANCIAL_MODE"
    elif mutation == "corrupt_decision":
        model["decisions"][-1]["decision"] = "GENERIC_MODEL_PROMOTION"
    elif mutation == "missing_digest":
        model["payload_sha256"] = None
    elif mutation == "missing_model_id":
        model["payload"]["model_id"] = "not-a-model-digest"
    out = _enrich(knowledge=provider)
    row = out["assets"][0]["learning_engine"]
    assert row["status"] == "I05_MODEL_REVIEW_REQUIRED"
    assert row["champion"] is None
    assert row["model_contribution"] == "NONE"
    assert out["assets"][0]["state"] == "RESEARCH_READY"
    assert "SECRET_FINANCIAL_MODE" not in str(out)


def test_invalid_fallback_never_replaces_valid_champion_without_i05_registry():
    provider = PublicKnowledge()
    provider.models[102]["state"] = "INVALIDATED"
    out = _enrich(knowledge=provider)
    row = out["assets"][0]["learning_engine"]
    assert row["status"] == "I05_CHAMPION_VERIFIED"
    assert row["champion"]["knowledge_id"] == 101
    assert row["fallback"] is None
    assert "I05_FALLBACK_NOT_VALIDATED" in row["reason_codes"]


def test_i05_release_can_report_fallback_already_selected_without_promoting():
    registry = LearningRegistry(release=_release(
        champion_knowledge_id=102, fallback_knowledge_id=102,
        release_event="AUTO_ROLLBACK",
    ))
    out = _enrich(registry=registry)
    row = out["assets"][0]["learning_engine"]
    assert row["status"] == "I05_FALLBACK_VERIFIED"
    assert row["champion"]["knowledge_id"] == 102
    assert row["monitoring"]["status"] == "RESEARCH_AUTO_ROLLBACK_RECORDED"
    assert row["monitoring"]["monitor_or_rollback_invoked"] is False
    assert registry.promotions == registry.monitor_runs == 0


def test_missing_i05_or_i04_dependencies_fail_closed(monkeypatch):
    monkeypatch.setattr(i05a, "_providers", lambda registry, knowledge: (None, None))
    out = i05a.enrich_research_with_i05(_public_i04(), now=NOW)
    assert out["integrations"]["i05"] == "NOT_CONNECTED"
    assert out["assets"][0]["learning_engine"]["champion"] is None
    broken = _public_i04()
    broken["integrations"]["i04"] = "NOT_CONNECTED"
    out = i05a.enrich_research_with_i05(broken, now=NOW,
                                      registry=LearningRegistry(),
                                      knowledge=PublicKnowledge())
    assert out["integrations"]["i05"] == "BLOCKED_I04_NOT_CONNECTED"
    assert out["assets"][0]["learning_engine"]["champion"] is None


def test_i05_disabled_has_zero_registry_queries():
    registry = LearningRegistry(enabled=False)
    out = _enrich(registry=registry)
    assert out["integrations"]["i05"] == "DISABLED"
    assert registry.queries == []


def test_invalid_profile_or_unverified_context_never_queries_registry():
    registry = LearningRegistry()
    source = _public_i04()
    source["assets"][0]["research_timeframes_seconds"]["lower"] = 120
    out = _enrich(registry=registry, source=source)
    assert out["assets"][0]["learning_engine"]["status"] == "I05_PUBLIC_SCOPE_UNVERIFIED"
    source = _public_i04()
    source["assets"][0]["context_consensus"] = "MIXED_OR_PENDING"
    out = _enrich(registry=registry, source=source)
    assert out["assets"][0]["learning_engine"]["status"] == "I05_PUBLIC_SCOPE_UNVERIFIED"
    assert registry.queries == []


def test_research_origin_is_public_never_demo_real_or_simulation():
    out = _enrich()
    assert out["assets"][0]["learning_engine"]["source_origin"] == (
        "PUBLIC_CLOSED_REGIME_RESEARCH"
    )
    assert out["assets"][0]["learning_engine"]["financial_source_used"] is False
    assert "DEMO" not in str(out["assets"][0]["learning_engine"])
    assert "REAL" not in str(out["assets"][0]["learning_engine"])
    assert "SIMULATION" not in str(out["assets"][0]["learning_engine"])


def test_i05_adapter_never_increases_evidence_points_or_approves_trading():
    source = _public_i04()
    source["assets"][0]["evidence_completeness_points"] = 20
    before = deepcopy(source)
    out = _enrich(source=source)
    assert out["assets"][0]["evidence_completeness_points"] == 20
    assert out["assets"][0]["state"] == before["assets"][0]["state"]
    assert out["orders_sent"] == 0
    assert out["approved_for_trading"] is False
    assert out["distribution"]["publish_allowed"] is False


def test_model_read_integrity_exception_is_explicit_and_no_model_used():
    class BrokenKnowledge(PublicKnowledge):
        def get_knowledge(self, *args, **kwargs):
            raise ValueError("I04 MODEL integrity changed")
    out = _enrich(knowledge=BrokenKnowledge())
    row = out["assets"][0]["learning_engine"]
    assert row["status"] == "I05_MODEL_REVIEW_REQUIRED"
    assert row["champion"] is None


def test_integrated_async_chain_calls_i04_orchestrator_once(monkeypatch):
    source = _public_i04()
    scanner = AsyncMock(return_value=source)
    monkeypatch.setattr(i04a, "orchestrate_research_with_i04", scanner)
    provider, registry = PublicKnowledge(), LearningRegistry()
    out = asyncio.run(i05a.orchestrate_research_with_i04_i05(
        previous={"markets": {}},
        prior_i06={"version": "intelligence2_i06_evidence_research_v1"},
        expiry_minutes=5, now=NOW,
        knowledge_provider=provider, registry_provider=registry,
    ))
    scanner.assert_awaited_once_with(
        previous={"markets": {}},
        prior_i06={"version": "intelligence2_i06_evidence_research_v1"},
        expiry_minutes=5, now=NOW, provider=provider,
    )
    assert out["assets"][0]["learning_engine"]["status"] == "I05_CHAMPION_VERIFIED"
    assert len(registry.queries) == 1


@pytest.mark.parametrize("bad", [True, 0, -10, float("nan"), "50000000"])
def test_invalid_clock_fails_before_i04_or_i05_calls(bad, monkeypatch):
    scan = AsyncMock(side_effect=AssertionError("cannot run"))
    monkeypatch.setattr(i04a, "orchestrate_research_with_i04", scan)
    with pytest.raises(ValueError):
        asyncio.run(i05a.orchestrate_research_with_i04_i05(now=bad))
    scan.assert_not_awaited()


@pytest.mark.parametrize("bad", [
    {"version": "wrong"},
    {"i04_adapter_version": "unknown"},
    {"execution_allowed": True},
    {"approved_for_trading": True},
    {"orders_sent": 1},
    {"user_id": 98},
    {"distribution": {"publish_allowed": True}},
])
def test_untrusted_financial_report_is_rejected_before_model_read(bad):
    source = _public_i04()
    source.update(bad)
    provider, registry = PublicKnowledge(), LearningRegistry()
    with pytest.raises(ValueError):
        i05a.enrich_research_with_i05(
            source, now=NOW, registry=registry, knowledge=provider,
        )
    assert registry.queries == []
    assert provider.model_reads == []


def test_excluded_asset_never_looks_up_models():
    source = _public_i04()
    source["assets"][0]["state"] = "EXCLUDED"
    registry = LearningRegistry()
    out = _enrich(source=source, registry=registry)
    assert out["assets"][0]["learning_engine"]["status"] == "SKIPPED_EXCLUDED_RESEARCH"
    assert registry.queries == []


def test_bounded_scope_queries_and_no_extra_cache():
    source = _public_i04()
    item = source["assets"][0]
    source["assets"] = [dict(item, symbol=f"FRX{i:05d}")
                        for i in range(i05a.MAX_ASSETS + 2)]
    class Many(LearningRegistry):
        def get_research_release(self, **kwargs):
            self.queries.append(kwargs)
            return {
                "version": i05a.I05_VERSION,
                "state": "NO_VALIDATED_RESEARCH_MODEL",
                "champion_knowledge_id": None,
                "fallback_knowledge_id": None,
                "execution_allowed": False, "approved_for_trading": False,
            }
    registry = Many()
    out = _enrich(source=source, registry=registry)
    assert len(registry.queries) == i05a.MAX_ASSETS
    assert out["i05_queried_asset_count"] == i05a.MAX_ASSETS
    assert out["assets"][-1]["learning_engine"]["status"] == "I05_CAPACITY_DEFERRED"
    assert out["assets"][-1]["state"] == "WAIT_RECHECK"

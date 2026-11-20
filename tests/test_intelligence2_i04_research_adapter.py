"""I04 read-only adapter tests: actual I04 contract shape, synthetic public data.

No production database, migration, broker, account, training or signals.
The I04 implementation lives on a separate branch; these fixtures exercise
the existing retrieve_active/get_knowledge public API without cloning it.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.intelligence2 import i04_research_adapter as i04a
from app.intelligence2 import research_orchestrator as orchestration
from app.intelligence2 import research_signal_engine as i06

NOW = 20_040
PROFILE = {"higher": 3600, "intermediate": 300, "lower": 60}


def _i03_source(*, direction="UP", symbol="FRXEURUSD"):
    now = NOW
    last = (now // 60 - 1) * 60
    contexts = {
        role: {
            "state": "UPTREND" if direction == "UP" else "DOWNTREND",
            "direction": direction, "data_quality": "COMPLETE",
            "execution_allowed": False, "observed_at_epoch": now,
            "timeframe_seconds": seconds,
            "last_closed_epoch": (now // seconds - 1) * seconds,
        }
        for role, seconds in PROFILE.items()
    }
    candidate = {
        "symbol": symbol, "market": "forex", "eligible": True,
        "execution_allowed": False, "score": 42.5,
        "strategy_family": "public_forex_observation",
        "research_state": "OBSERVED",
        "candle_quality": "COMPLETE", "closed_candles": 96,
        "last_closed_epoch": last,
        "timeframe_inputs": {
            "granularity_seconds": 60, "last_closed_epoch": last,
        },
        "context_timeframe": {
            "version": "intelligence2_timeframes_v1",
            "provenance_sha256": "a" * 64,
            "observed_at_epoch": now,
            "execution_allowed": False, "approved_for_trading": False,
            "confidence_kind": "HEURISTIC_NOT_PROBABILISTIC",
            "profile": {
                "version": "intelligence2_timeframes_v1",
                "higher_seconds": 3600, "intermediate_seconds": 300,
                "lower_seconds": 60, "expiry_minutes": 5,
                "broker_contract_expiry_verified": False,
            },
            "alignment": {
                "state": "ALIGNED_RESEARCH", "direction": direction,
                "strategy_research_usable": True,
            },
            "contexts": contexts,
        },
    }
    return {
        "version": "intelligence2_i03_v1",
        "i02_source_version": "intelligence2_i02_v1",
        "observed_at_epoch": now, "orders_sent": 0,
        "execution_allowed": False, "approved_for_trading": False,
        "confidence_kind": "HEURISTIC_NOT_PROBABILISTIC",
        "scan_coverage_complete": True,
        "context_coverage_complete": True,
        "markets": {"forex": {"selected": [candidate]}},
    }


def _report(*, direction="UP", symbol="FRXEURUSD"):
    i06_report = i06.assess_research_report(
        _i03_source(direction=direction, symbol=symbol), now=NOW,
    )
    report = orchestration.consolidate_research_report(i06_report, now=NOW)
    assert report["assets"][0]["state"] == "RESEARCH_READY"
    return report


def _date(value):
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _knowledge(category="MARKET", *, knowledge_id=101, direction="UP",
               state="ACTIVE", valid_until=None, next_review=None,
               timeframe_seconds=300, strategy=None):
    valid_until = valid_until if valid_until is not None else NOW + 86_400
    next_review = next_review if next_review is not None else NOW + 20_000
    family = strategy if strategy is not None else "public_forex_observation"
    payload = {"direction": direction, "data_quality": "COMPLETE"}
    if category == "STRATEGY":
        payload["strategy_family"] = family
    core = {
        "id": knowledge_id,
        "version": i04a.I04_VERSION,
        "category": category,
        "market": "forex",
        "symbol": "FRXEURUSD",
        "timeframe_seconds": timeframe_seconds,
        "subject": "public_context",
        "model_version": "i03_v1",
        "regime": "UPTREND",
        "state": state,
        "revision": 5,
        "payload": payload,
        "payload_sha256": "b" * 64,
        "valid_from": _date(NOW - 10_000),
        "valid_until": _date(valid_until),
        "next_review_at": _date(next_review),
        "reuse_status": "CURRENT_VERIFIED_ACTIVE",
        "reusable_for_research": True,
        "approved_for_trading": False,
        "execution_allowed": False,
    }
    detail = {
        **core,
        "evidence": [
            {"origin_group": "PUBLIC_FEED_ALPHA", "observations": 144, "verified": True,
             "source_sha256": "c" * 64},
            {"origin_group": "PUBLIC_FEED_BETA", "observations": 192, "verified": True,
             "source_sha256": "d" * 64},
        ],
        "decisions": [
            {"revision": 1, "to_state": "DISCOVERED"},
            {"revision": 5, "to_state": "ACTIVE"},
        ],
    }
    return core, detail


class FakeI04:
    VERSION = i04a.I04_VERSION

    def __init__(self, records=None, *, enabled=True, excluded=None):
        self.is_enabled = enabled
        self.records = records if records is not None else [
            _knowledge("MARKET"), _knowledge("STRATEGY", knowledge_id=102),
        ]
        self.excluded = excluded or {}
        self.queries = []
        self.details_read = []

    def enabled(self):
        return self.is_enabled

    def retrieve_active(self, *, category, market, symbol,
                        observed_regime, now, limit):
        self.queries.append((category, market, symbol, observed_regime, now, limit))
        assert now.tzinfo == timezone.utc
        assert limit == i04a.MAX_RECORDS_PER_CATEGORY
        assert market == "forex"
        assert symbol == "FRXEURUSD"
        assert observed_regime in ("UPTREND", "DOWNTREND")
        return {
            "version": self.VERSION,
            "category": category, "market": market, "symbol": symbol,
            "observed_regime": observed_regime,
            "read_only": True, "execution_allowed": False,
            "active": [r for r, _ in self.records if r["category"] == category],
            "excluded": self.excluded.get(category, []),
        }

    def get_knowledge(self, knowledge_id, *, now, observed_regime,
                      include_lineage):
        self.details_read.append(knowledge_id)
        assert include_lineage is True
        assert now.tzinfo == timezone.utc
        for record, detail in self.records:
            if detail["id"] == knowledge_id:
                return deepcopy(detail)
        raise AssertionError("missing synthetic knowledge id")


def test_read_existing_i04_market_and_strategy_evidence_without_writes():
    provider = FakeI04()
    result = i04a.enrich_research_with_i04(_report(), now=NOW, provider=provider)
    asset = result["assets"][0]
    kb = asset["knowledge_base"]
    assert result["state"] == "RESEARCH_CANDIDATES"
    assert asset["state"] == "RESEARCH_READY"
    assert result["i04_enrichment_status"] == "READ_ONLY_CONNECTED"
    assert result["integrations"]["i04"] == "READ_ONLY_CONNECTED"
    assert result["integrations"]["i05"] == "NOT_CONNECTED"
    assert [q[0] for q in provider.queries] == ["MARKET", "STRATEGY"]
    assert provider.details_read == [101, 102]
    assert kb["status"] == "I04_CURRENT_ACTIVE_VERIFIED"
    assert kb["consulted_count"] == 2
    assert kb["used_count"] == 2
    assert [x["knowledge_id"] for x in kb["used_public_knowledge"]] == [101, 102]
    assert [x["verified_origin_count"] for x in kb["used_public_knowledge"]] == [2, 2]
    assert all(x["contribution"] == "PUBLIC_CONTEXT_CORROBORATION"
               for x in kb["used_public_knowledge"])
    assert "I04_PUBLIC_KNOWLEDGE_CORROBORATED" in asset["reason_codes"]
    assert result["orders_sent"] == 0
    assert result["execution_allowed"] is False
    assert result["distribution"]["publish_allowed"] is False
    assert "payload" not in str(kb)
    assert "account_id" not in str(result)


def test_missing_i04_import_is_explicit_and_neutral(monkeypatch):
    monkeypatch.setattr(i04a, "_load_provider", lambda provider: None)
    base = _report()
    result = i04a.enrich_research_with_i04(base, now=NOW)
    assert result["assets"][0]["state"] == base["assets"][0]["state"]
    assert result["assets"][0]["knowledge_base"]["status"] == "NOT_CONNECTED"
    assert result["integrations"]["i04"] == "NOT_CONNECTED"


def test_disabled_i04_makes_no_database_read():
    provider = FakeI04(enabled=False)
    out = i04a.enrich_research_with_i04(_report(), now=NOW, provider=provider)
    assert out["i04_enrichment_status"] == "DISABLED"
    assert provider.queries == []
    assert out["assets"][0]["knowledge_base"]["used_count"] == 0


def test_existing_i04_overdue_and_invalidated_knowledge_is_excluded():
    provider = FakeI04(
        records=[],
        excluded={"MARKET": [
            {"id": 11, "state": "REVIEW_REQUIRED", "reason": "PERIODIC_REVIEW_DUE"},
            {"id": 12, "state": "INVALIDATED", "reason": "INACTIVE_STATE"},
            {"id": 13, "state": "ACTIVE", "reason": "VALIDITY_EXPIRED"},
        ]},
    )
    out = i04a.enrich_research_with_i04(_report(), now=NOW, provider=provider)
    asset = out["assets"][0]
    assert asset["state"] == "RESEARCH_READY"
    kb = asset["knowledge_base"]
    assert kb["status"] == "I04_NO_REUSABLE_KNOWLEDGE"
    assert kb["used_count"] == 0
    assert [r["reason"] for r in kb["excluded_public_knowledge"]] == [
        "PERIODIC_REVIEW_DUE", "INACTIVE_STATE", "VALIDITY_EXPIRED",
    ]


@pytest.mark.parametrize("alter,reason", [
    ("expired", "I04_REVIEW_REQUIRED_OR_EXPIRED"),
    ("review_due", "I04_REVIEW_REQUIRED_OR_EXPIRED"),
    ("incompatible_timeframe", "I04_TIMEFRAME_NOT_RELEVANT"),
    ("revision_mismatch", "I04_STATE_OR_IDENTITY_MISMATCH"),
    ("insufficient_evidence", "I04_VERIFIED_ORIGINS_INSUFFICIENT"),
    ("tampered_lineage", "I04_EVIDENCE_OR_HISTORY_UNAVAILABLE"),
    ("private_detail", "PRIVATE_KNOWLEDGE_REFUSED"),
    ("private_payload", "PRIVATE_KNOWLEDGE_REFUSED"),
    ("strategy_mismatch", "I04_STRATEGY_NOT_RELEVANT"),
])
def test_stale_unverified_private_or_inapplicable_knowledge_never_influences(
    alter, reason,
):
    category = "STRATEGY" if alter == "strategy_mismatch" else "MARKET"
    core, detail = _knowledge(category, knowledge_id=101)
    if alter == "expired":
        detail["valid_until"] = _date(NOW)
        core["valid_until"] = detail["valid_until"]
    elif alter == "review_due":
        detail["next_review_at"] = _date(NOW)
        core["next_review_at"] = detail["next_review_at"]
    elif alter == "incompatible_timeframe":
        detail["timeframe_seconds"] = 120
    elif alter == "revision_mismatch":
        detail["revision"] = 99
    elif alter == "insufficient_evidence":
        detail["evidence"] = detail["evidence"][:1]
    elif alter == "tampered_lineage":
        detail["decisions"][-1]["revision"] = 3
    elif alter == "private_detail":
        detail["account_id"] = "SECRET_DO_NOT_COPY"
    elif alter == "private_payload":
        detail["payload"]["user_id"] = "SECRET_DO_NOT_COPY"
    elif alter == "strategy_mismatch":
        detail["payload"]["strategy_family"] = "wrong_strategy"
    provider = FakeI04(records=[(core, detail)])
    out = i04a.enrich_research_with_i04(_report(), now=NOW, provider=provider)
    kb = out["assets"][0]["knowledge_base"]
    assert kb["used_count"] == 0
    assert any(r["reason"] == reason for r in kb["excluded_public_knowledge"])
    assert out["assets"][0]["state"] == "RESEARCH_READY"
    assert "SECRET_DO_NOT_COPY" not in str(out)


def test_conflicting_active_direction_downgrades_public_research_for_recheck():
    core, detail = _knowledge(direction="DOWN")
    provider = FakeI04(records=[(core, detail)])
    out = i04a.enrich_research_with_i04(_report(), now=NOW, provider=provider)
    asset = out["assets"][0]
    assert out["state"] == "WAIT_RECHECK"
    assert asset["state"] == "WAIT_RECHECK"
    assert out["counts"]["RESEARCH_READY"] == 0
    assert asset["knowledge_base"]["status"] == "I04_CONFLICT_RECHECK"
    assert asset["knowledge_base"]["used_count"] == 0
    assert "I04_ACTIVE_CONTEXT_CONFLICT" in asset["reason_codes"]


def test_provider_unavailable_fails_closed_no_partial_knowledge_influence():
    class BadI04(FakeI04):
        def retrieve_active(self, **kwargs):
            if kwargs["category"] == "STRATEGY":
                raise RuntimeError("I04 database not migrated")
            return super().retrieve_active(**kwargs)
    out = i04a.enrich_research_with_i04(_report(), now=NOW, provider=BadI04())
    asset = out["assets"][0]
    assert out["integrations"]["i04"] == "READ_ONLY_PARTIAL"
    assert asset["state"] == "WAIT_RECHECK"
    assert asset["knowledge_base"]["status"] == "I04_UNAVAILABLE"
    assert asset["knowledge_base"]["used_count"] == 0
    assert "I04_READ_UNAVAILABLE" in asset["reason_codes"]


def test_wrong_i04_contract_or_untrusted_reply_not_used():
    provider = FakeI04()
    provider.VERSION = "wrong"
    out = i04a.enrich_research_with_i04(_report(), now=NOW, provider=provider)
    assert out["i04_enrichment_status"] == "INCOMPATIBLE_I04_CONTRACT"
    assert provider.queries == []
    class Liar(FakeI04):
        def retrieve_active(self, **kwargs):
            out = super().retrieve_active(**kwargs)
            out["read_only"] = False
            return out
    report = i04a.enrich_research_with_i04(_report(), now=NOW, provider=Liar())
    assert report["assets"][0]["state"] == "WAIT_RECHECK"
    assert report["assets"][0]["knowledge_base"]["used_count"] == 0


def test_no_verified_public_context_skips_i04_lookup():
    base = _report()
    base["assets"][0]["context_consensus"] = "MIXED_OR_PENDING"
    provider = FakeI04()
    out = i04a.enrich_research_with_i04(base, now=NOW, provider=provider)
    assert provider.queries == []
    assert out["assets"][0]["knowledge_base"]["status"] == "I04_CONTEXT_NOT_VERIFIED"


def test_missing_strategy_skips_strategy_lookup_without_fabrication():
    base = _report()
    base["assets"][0]["strategy_family"] = None
    provider = FakeI04()
    out = i04a.enrich_research_with_i04(base, now=NOW, provider=provider)
    assert [q[0] for q in provider.queries] == ["MARKET"]
    assert out["assets"][0]["knowledge_base"]["used_count"] == 1


def test_excluded_i06_asset_does_not_access_i04():
    source = _i03_source()
    source["markets"]["forex"]["selected"][0]["candle_quality"] = "STALE"
    report = orchestration.consolidate_research_report(
        i06.assess_research_report(source, now=NOW), now=NOW,
    )
    provider = FakeI04()
    out = i04a.enrich_research_with_i04(report, now=NOW, provider=provider)
    assert out["assets"][0]["state"] == "EXCLUDED"
    assert out["assets"][0]["knowledge_base"]["status"] == "SKIPPED_EXCLUDED_RESEARCH"
    assert provider.queries == []


def test_bounded_i04_queries_defer_over_capacity_without_extra_storage():
    report = _report()
    report["assets"] = [dict(report["assets"][0], symbol=f"FRX{i:05d}")
                        for i in range(i04a.MAX_ASSETS + 2)]
    class Many(FakeI04):
        def retrieve_active(self, **kwargs):
            self.queries.append(kwargs)
            return {
                "version": self.VERSION,
                "category": kwargs["category"],
                "market": kwargs["market"], "symbol": kwargs["symbol"],
                "observed_regime": kwargs["observed_regime"],
                "read_only": True, "execution_allowed": False,
                "active": [], "excluded": [],
            }
    provider = Many()
    out = i04a.enrich_research_with_i04(report, now=NOW, provider=provider)
    assert out["i04_queried_asset_count"] == i04a.MAX_ASSETS
    assert len(provider.queries) == i04a.MAX_ASSETS * 2
    assert out["assets"][-1]["knowledge_base"]["status"] == "I04_CAPACITY_DEFERRED"
    assert out["assets"][-1]["state"] == "WAIT_RECHECK"
    assert out["counts"]["WAIT_RECHECK"] == 2


def test_read_only_async_integration_reuses_existing_orchestrator_once(monkeypatch):
    report = _report()
    scan = AsyncMock(return_value=report)
    monkeypatch.setattr(orchestration, "orchestrate_public_research", scan)
    provider = FakeI04()
    out = asyncio.run(i04a.orchestrate_research_with_i04(
        previous={"markets": {}}, prior_i06={"version": i06.VERSION},
        expiry_minutes=5, now=NOW, provider=provider,
    ))
    scan.assert_awaited_once_with(
        previous={"markets": {}}, prior_i06={"version": i06.VERSION},
        expiry_minutes=5, now=NOW,
    )
    assert out["assets"][0]["knowledge_base"]["used_count"] == 2
    assert out["orders_sent"] == 0


@pytest.mark.parametrize("bad", [
    {"version": "wrong"},
    {"execution_allowed": True},
    {"approved_for_trading": True},
    {"orders_sent": 1},
    {"user_id": 90},
])
def test_untrusted_financial_report_fails_closed_before_i04_reads(bad):
    source = _report()
    source.update(bad)
    provider = FakeI04()
    with pytest.raises(ValueError):
        i04a.enrich_research_with_i04(source, now=NOW, provider=provider)
    assert provider.queries == []


def test_invalid_clock_is_rejected_before_read():
    with pytest.raises(ValueError):
        i04a.enrich_research_with_i04(_report(), now=True, provider=FakeI04())


def test_full_i04_source_is_unavailable_until_separate_branch_is_integrated():
    # This check uses only I04's published API shapes: no I04 implementation
    # or schema is copied into this PR.
    provider = FakeI04()
    assert provider.VERSION == "intelligence2_knowledge_base_v1"
    assert hasattr(provider, "retrieve_active")
    assert hasattr(provider, "get_knowledge")

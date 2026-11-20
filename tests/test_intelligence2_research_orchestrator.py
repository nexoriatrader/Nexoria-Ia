"""Research orchestration integration: synthetic I01/I02/I03 -> I06, no orders."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app import asset_scanner
from app.intelligence2 import research_orchestrator as orchestration
from app.intelligence2 import research_signal_engine as i06

NOW = 20_040
ROLES = {"higher": 3600, "intermediate": 300, "lower": 60}


def _i03_source(*, symbol="FRXEURUSD", direction="UP", observed=NOW):
    last_closed = (observed // 60 - 1) * 60
    asset = {
        "symbol": symbol, "market": "forex",
        "eligible": True, "execution_allowed": False,
        "score": 41.5, "strategy_family": "public_forex_observation",
        "research_state": "OBSERVED", "candle_quality": "COMPLETE",
        "closed_candles": 96, "last_closed_epoch": last_closed,
        "timeframe_inputs": {
            "granularity_seconds": 60, "last_closed_epoch": last_closed,
        },
        "context_timeframe": {
            "version": "intelligence2_timeframes_v1",
            "provenance_sha256": "a" * 64,
            "observed_at_epoch": observed,
            "execution_allowed": False, "approved_for_trading": False,
            "confidence_kind": "HEURISTIC_NOT_PROBABILISTIC",
            "profile": {
                "version": "intelligence2_timeframes_v1",
                "higher_seconds": 3600, "intermediate_seconds": 300,
                "lower_seconds": 60, "expiry_minutes": 5,
                "broker_contract_expiry_verified": False,
            },
            "alignment": {
                "state": "ALIGNED_RESEARCH",
                "direction": direction,
                "strategy_research_usable": True,
            },
            "contexts": {
                role: {
                    "state": "UPTREND" if direction == "UP" else "DOWNTREND",
                    "direction": direction,
                    "data_quality": "COMPLETE",
                    "execution_allowed": False,
                    "observed_at_epoch": observed,
                    "timeframe_seconds": seconds,
                    "last_closed_epoch": (observed // seconds - 1) * seconds,
                }
                for role, seconds in ROLES.items()
            },
        },
    }
    return {
        "version": "intelligence2_i03_v1",
        "i02_source_version": "intelligence2_i02_v1",
        "observed_at_epoch": observed,
        "orders_sent": 0, "execution_allowed": False,
        "approved_for_trading": False,
        "confidence_kind": "HEURISTIC_NOT_PROBABILISTIC",
        "scan_coverage_complete": True, "context_coverage_complete": True,
        "markets": {"forex": {"selected": [asset]}},
    }


def _i06(*, source=None, previous=None, now=NOW):
    return i06.assess_research_report(
        source if source is not None else _i03_source(),
        previous=previous, now=now,
    )


def _only(report):
    assert len(report["assets"]) == 1
    return report["assets"][0]


def test_full_public_chain_yields_traceable_data_ready_asset_not_trade_signal():
    report = orchestration.consolidate_research_report(_i06(), now=NOW)
    asset = _only(report)
    assert report["version"] == orchestration.VERSION
    assert report["state"] == "RESEARCH_CANDIDATES"
    assert report["source_versions"] == {
        "i02": i06.I02_VERSION, "i03": i06.SOURCE_VERSION,
        "i06": i06.VERSION,
    }
    assert report["counts"] == {
        "RESEARCH_READY": 1, "WAIT_RECHECK": 0, "EXCLUDED": 0,
    }
    assert asset["state"] == "RESEARCH_READY"
    assert asset["observed_trend"] == "UP"
    assert asset["context_consensus"] == "THREE_TIMEFRAMES"
    assert asset["evidence_strength"] == "CONSISTENT"
    assert asset["evidence_completeness_points"] == 100
    assert asset["valid_until_epoch"] == NOW + 60
    assert asset["source_modules"] == ("I01", "I02", "I03", "I06")
    assert len(asset["fingerprint"]) == 64
    assert "PUBLIC_DATA_READY_FOR_STUDY" in asset["reason_codes"]
    assert asset["distribution"]["publish_allowed"] is False
    assert report["distribution"]["publish_allowed"] is False
    assert report["orders_sent"] == 0
    assert report["execution_allowed"] is False
    assert report["approved_for_trading"] is False
    assert report["integrations"] == {"i04": "NOT_CONNECTED", "i05": "NOT_CONNECTED"}
    assert "trade_side" not in asset
    assert "entry_epoch" not in asset
    assert "contract_expiry_minutes" not in asset


def test_downward_public_trend_is_observed_not_contract_direction():
    result = orchestration.consolidate_research_report(
        _i06(source=_i03_source(direction="DOWN")), now=NOW,
    )
    asset = _only(result)
    assert asset["observed_trend"] == "DOWN"
    assert asset["state"] == "RESEARCH_READY"
    assert asset["distribution"]["publish_allowed"] is False


def test_missing_timeframe_profile_is_wait_not_fabricated_research_candidate():
    source = _i03_source()
    del source["markets"]["forex"]["selected"][0]["context_timeframe"]["profile"]
    asset = _only(orchestration.consolidate_research_report(
        _i06(source=source), now=NOW,
    ))
    assert asset["state"] == "WAIT_RECHECK"
    assert asset["evidence_completeness_points"] < 100
    assert "RESEARCH_PREREQUISITES_INCOMPLETE" in asset["reason_codes"]


@pytest.mark.parametrize("damage,reason", [
    ("candle_gapped", "I01_DATA_INCOMPLETE"),
    ("candle_stale", "I02_LAST_CLOSED_STALE"),
    ("strong_conflict", "I03_CONTEXT_NOT_STABLE"),
    ("opposite_timeframe", "I03_DIRECTION_CONFLICT"),
    ("open_candle", "I02_OPEN_CANDLE_NOT_CONFIRMED"),
])
def test_invalid_data_or_context_conflict_is_excluded(damage, reason):
    source = _i03_source()
    asset = source["markets"]["forex"]["selected"][0]
    if damage == "candle_gapped":
        asset["candle_quality"] = "GAPPED"
    if damage == "candle_stale":
        asset["last_closed_epoch"] -= 60
        asset["timeframe_inputs"]["last_closed_epoch"] -= 60
    if damage == "strong_conflict":
        asset["context_timeframe"]["alignment"]["state"] = "BLOCKED_STRONG_CONFLICT"
    if damage == "opposite_timeframe":
        asset["context_timeframe"]["contexts"]["lower"]["direction"] = "DOWN"
    if damage == "open_candle":
        asset["last_closed_epoch"] += 60
        asset["timeframe_inputs"]["last_closed_epoch"] += 60
    result = orchestration.consolidate_research_report(
        _i06(source=source), now=NOW,
    )
    out = _only(result)
    assert out["state"] == "EXCLUDED"
    assert reason in out["reason_codes"]
    assert result["counts"]["RESEARCH_READY"] == 0
    assert out["distribution"]["publish_allowed"] is False


def test_expired_source_excludes_all_assets_even_if_evidence_was_consistent():
    base = _i06()
    result = orchestration.consolidate_research_report(base, now=NOW + 60)
    assert result["state"] == "SOURCE_EXPIRED"
    assert _only(result)["state"] == "EXCLUDED"
    assert result["counts"]["RESEARCH_READY"] == 0


def test_no_selected_assets_never_fabricates_a_research_candidate():
    source = _i03_source()
    source["markets"]["forex"]["selected"] = []
    result = orchestration.consolidate_research_report(_i06(source=source), now=NOW)
    assert result["assets"] == []
    assert result["asset_count"] == 0
    assert result["state"] == "NO_VALID_RESEARCH"


def test_unchanged_public_observation_requires_recheck_not_republication():
    first = _i06()
    second = _i06(previous=first)
    asset = _only(orchestration.consolidate_research_report(second, now=NOW))
    assert asset["state"] == "WAIT_RECHECK"
    assert "UNCHANGED_PUBLIC_OBSERVATION" in asset["reason_codes"]
    assert asset["distribution"]["publish_allowed"] is False


def test_changed_candle_fingerprint_is_new_research_version():
    first = _i06()
    source = _i03_source()
    asset = source["markets"]["forex"]["selected"][0]
    asset["context_timeframe"]["provenance_sha256"] = "b" * 64
    newer = _i06(source=source, previous=first)
    result = orchestration.consolidate_research_report(newer, now=NOW)
    assert _only(result)["lifecycle"] == "UPDATED"
    assert _only(result)["state"] == "RESEARCH_READY"


def test_duplicate_same_asset_in_upstream_report_is_excluded_once():
    report = _i06()
    report["research_records"].append(deepcopy(report["research_records"][0]))
    result = orchestration.consolidate_research_report(report, now=NOW)
    assert result["asset_count"] == 1
    assert _only(result)["state"] == "EXCLUDED"
    assert "DUPLICATED_ASSET_IN_SOURCE" in _only(result)["reason_codes"]


@pytest.mark.parametrize("field,bad", [
    ("version", "unknown"),
    ("source_version", "unknown"),
    ("i02_source_version", "unknown"),
    ("orders_sent", 1),
    ("execution_allowed", True),
    ("approved_for_trading", True),
    ("trade_signal_approval_supported", True),
    ("distribution", {"publish_allowed": True}),
    ("user_id", 451),
])
def test_untrusted_or_user_specific_source_fails_closed(field, bad):
    report = _i06()
    report[field] = bad
    result = orchestration.consolidate_research_report(report, now=NOW)
    assert result["state"] == "SOURCE_UNTRUSTED"
    assert result["assets"] == []
    assert result["distribution"]["publish_allowed"] is False


@pytest.mark.parametrize("field,bad", [
    ("trade_side", "CALL"),
    ("entry_epoch", NOW),
    ("contract_expiry_minutes", 5),
    ("execution_allowed", True),
    ("approved_for_trading", True),
    ("distribution", {"publish_allowed": True}),
    ("user_id", 23),
    ("fingerprint", "invalid"),
    ("decision", "SIGNAL APPROVED"),
])
def test_forged_record_is_excluded_without_sending_operations(field, bad):
    source = _i06()
    source["research_records"][0][field] = bad
    result = orchestration.consolidate_research_report(source, now=NOW)
    asset = _only(result)
    assert asset["state"] == "EXCLUDED"
    assert result["counts"]["RESEARCH_READY"] == 0
    assert asset["distribution"]["publish_allowed"] is False
    assert "user_id" not in asset
    assert "trade_side" not in asset


def test_more_than_existing_i06_public_capacity_fails_closed():
    report = _i06()
    report["research_records"] *= i06.MAX_ITEMS + 1
    result = orchestration.consolidate_research_report(report, now=NOW)
    assert result["state"] == "SOURCE_UNTRUSTED"
    assert result["asset_count"] == 0


def test_orchestrator_calls_existing_i06_facade_exactly_once(monkeypatch):
    scan = AsyncMock(return_value=_i06())
    monkeypatch.setattr(asset_scanner, "scan_intelligence_research", scan)
    result = asyncio.run(orchestration.orchestrate_public_research(
        previous={"markets": {}}, prior_i06={"version": i06.VERSION},
        expiry_minutes=5, now=NOW,
    ))
    scan.assert_awaited_once_with(
        previous={"markets": {}}, prior_i06={"version": i06.VERSION},
        expiry_minutes=5, now=NOW,
    )
    assert _only(result)["state"] == "RESEARCH_READY"
    assert result["orders_sent"] == 0


@pytest.mark.parametrize("wrong", [True, -1, 0, float("nan"), "100"])
def test_invalid_observation_clock_rejected_before_scanner_call(wrong, monkeypatch):
    scan = AsyncMock(side_effect=AssertionError("should not scan"))
    monkeypatch.setattr(asset_scanner, "scan_intelligence_research", scan)
    with pytest.raises(ValueError):
        asyncio.run(orchestration.orchestrate_public_research(now=wrong))
    scan.assert_not_awaited()


def test_private_extra_fields_are_not_copied_to_orchestrated_output():
    report = _i06()
    report["research_records"][0]["private_comment"] = "DO_NOT_LEAK"
    safe = orchestration.consolidate_research_report(report, now=NOW)
    assert "DO_NOT_LEAK" not in str(safe)
    assert _only(safe)["state"] == "RESEARCH_READY"

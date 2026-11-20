"""I06 public research evidence lifecycle; never a financial signal executor.

Consumes I01/I02/I03 *already computed* public observations. No network, DB,
market feed, ML, trading authorization, contract side, or entry-time calls.
Assessment strength describes consistency of evidence, NOT outcome probability.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time

VERSION = "intelligence2_i06_evidence_research_v1"
SOURCE_VERSION = "intelligence2_i03_v1"
I02_VERSION = "intelligence2_i02_v1"
HEURISTIC = "HEURISTIC_NOT_PROBABILISTIC"
_VALID_SYMBOL = re.compile(r"[A-Za-z0-9_]{2,30}\Z")
_VALID_SHA = re.compile(r"[a-f0-9]{64}\Z")
MAX_ITEMS = 48
RESEARCH_TTL_SECONDS = 60
_RESEARCH_ALIGNMENTS = frozenset(("ALIGNED_RESEARCH", "REVERSAL_CONFIRMED_RESEARCH"))
_RECHECK_ALIGNMENTS = frozenset(("WAIT_REEVALUATE", "NOT_EVALUATED_CAPACITY"))
_ROLES = ("higher", "intermediate", "lower")


def _numeric(value: object) -> float | None:
    if type(value) not in (float, int):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _fingerprint(source: dict, market: str, asset: dict) -> str:
    context = asset.get("context_timeframe")
    provenance = context.get("provenance_sha256") if isinstance(context, dict) else None
    observed = context.get("observed_at_epoch") if isinstance(context, dict) else None
    material = {
        "i03_version": source.get("version"),
        "i02_version": source.get("i02_source_version"),
        "market": market,
        "symbol": asset["symbol"],
        "provenance": provenance if isinstance(provenance, str) and _VALID_SHA.fullmatch(provenance) else None,
        "observed_at_epoch": observed if type(observed) is int else None,
        "research_state": asset.get("research_state"),
        "strategy_family": asset.get("strategy_family"),
        "quality": asset.get("candle_quality"),
        "last_closed_epoch": asset.get("last_closed_epoch"),
        "alignment_state": (
            context.get("alignment", {}).get("state")
            if isinstance(context, dict)
            and isinstance(context.get("alignment"), dict) else None
        ),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _previous_items(previous: dict | None) -> dict[tuple[str, str], dict]:
    if not isinstance(previous, dict) or previous.get("version") != VERSION:
        return {}
    records = previous.get("research_records")
    if not isinstance(records, list):
        return {}
    return {
        (record["market"], record["symbol"]): record
        for record in records[:MAX_ITEMS]
        if isinstance(record, dict)
        and isinstance(record.get("market"), str)
        and isinstance(record.get("symbol"), str)
    }


def _evidence(asset: dict) -> tuple[bool, str, list[str]]:
    """Validate independent public I01/I02/I03 facts without interpreting scores."""
    problems = []
    ctx = asset.get("context_timeframe")
    if not isinstance(ctx, dict):
        return False, "INVALID", ["I03_CONTEXT_MISSING"]
    if asset.get("eligible") is not True or asset.get("execution_allowed") is not False:
        problems.append("I02_RESEARCH_INPUT_UNTRUSTED")
    if asset.get("candle_quality") != "COMPLETE":
        problems.append("I01_DATA_INCOMPLETE")
    if type(asset.get("closed_candles")) is not int or asset["closed_candles"] < 2:
        problems.append("I01_CANDLE_HISTORY_MISSING")
    if ctx.get("execution_allowed") is not False or ctx.get("approved_for_trading") is not False:
        problems.append("I03_NONEXECUTION_GUARD_MISSING")
    if ctx.get("confidence_kind") != HEURISTIC:
        problems.append("I03_EVIDENCE_TYPE_UNVERIFIED")
    contexts = ctx.get("contexts")
    if not isinstance(contexts, dict) or any(
        role not in contexts or not isinstance(contexts[role], dict)
        or contexts[role].get("data_quality") != "COMPLETE"
        or contexts[role].get("execution_allowed") is not False
        for role in _ROLES
    ):
        problems.append("I03_TIMEFRAME_DATA_INVALID")
    alignment = ctx.get("alignment")
    state = alignment.get("state") if isinstance(alignment, dict) else ctx.get("state")
    if state not in (_RESEARCH_ALIGNMENTS | _RECHECK_ALIGNMENTS):
        problems.append("I03_CONTEXT_NOT_STABLE")
    if state == "ALIGNED_RESEARCH" and isinstance(contexts, dict):
        expected = alignment.get("direction") if isinstance(alignment, dict) else None
        if expected not in ("UP", "DOWN") or any(
            not isinstance(contexts.get(role), dict)
            or contexts[role].get("direction") != expected
            for role in _ROLES
        ):
            problems.append("I03_DIRECTION_CONFLICT")
        if not isinstance(alignment, dict) or alignment.get("strategy_research_usable") is not True:
            problems.append("I03_RESEARCH_ALIGNMENT_UNUSABLE")
    if not isinstance(asset.get("strategy_family"), str) or not asset["strategy_family"].strip():
        problems.append("I02_STRATEGY_UNVERIFIED")
    if not isinstance(asset.get("research_state"), str) or not asset["research_state"].strip():
        problems.append("I02_STRATEGY_EVIDENCE_MISSING")
    magnitude = _numeric(asset.get("score"))
    if magnitude is None or magnitude < 0:
        problems.append("I02_MEASUREMENT_INVALID")
    if problems:
        return False, "INVALID", sorted(set(problems))
    # These are qualitative categories, never probabilities or trade thresholds.
    if state in _RESEARCH_ALIGNMENTS:
        return True, "CONSISTENT", ["PUBLIC_EVIDENCE_COHERENT", "HEURISTIC_NOT_PROBABILITY"]
    return True, "INCONCLUSIVE", ["RECHECK_AFTER_NEW_CLOSED_CANDLE"]



# Read-only research evaluations; these are NOT trading-side or entry models.
def describe_public_context(asset: dict) -> dict:
    """Describe the pre-existing I03 trend alignment, without CALL/PUT mapping."""
    result = asset.get("context_timeframe")
    if not isinstance(result, dict):
        return {"observed_trend": "NEUTRAL", "consensus": "UNAVAILABLE"}
    alignment = result.get("alignment")
    contexts = result.get("contexts")
    if not isinstance(alignment, dict) or not isinstance(contexts, dict):
        return {"observed_trend": "NEUTRAL", "consensus": "UNAVAILABLE"}
    trend = alignment.get("direction")
    directions = [
        contexts.get(role, {}).get("direction")
        if isinstance(contexts.get(role), dict) else None
        for role in _ROLES
    ]
    if (
        alignment.get("state") == "ALIGNED_RESEARCH"
        and trend in ("UP", "DOWN")
        and all(value == trend for value in directions)
    ):
        return {"observed_trend": trend, "consensus": "THREE_TIMEFRAMES"}
    if any(value in ("UP", "DOWN") for value in directions):
        return {"observed_trend": "NEUTRAL", "consensus": "MIXED_OR_PENDING"}
    return {"observed_trend": "NEUTRAL", "consensus": "NO_DIRECTION"}


def review_public_freshness(asset: dict, *, observed_at: int, now: int) -> dict:
    """Use I02's closed-candle provenance, not a prediction of entry timing."""
    last = asset.get("last_closed_epoch")
    if type(last) is not int or last <= 0:
        return {"status": "UNVERIFIED", "reason_code": "I02_LAST_CLOSED_MISSING"}
    if last > observed_at or last > now:
        return {"status": "INVALID", "reason_code": "I02_LAST_CLOSED_IN_FUTURE"}
    if now - last > 120:
        return {"status": "STALE", "reason_code": "I02_LAST_CLOSED_STALE"}
    return {"status": "CURRENT", "reason_code": "I02_CLOSED_CANDLE_CURRENT"}


def detect_public_research_pattern(asset: dict, *,
                                   evidence_valid: bool,
                                   evidence_class: str) -> dict:
    """Identify only a documented observational pattern, never an entry."""
    context = describe_public_context(asset)
    if not evidence_valid:
        return {"state": "INVALID_DATA", "reason_code": "SOURCE_VALIDATION_FAILED"}
    if context["consensus"] != "THREE_TIMEFRAMES":
        return {"state": "NO_CONSENSUS", "reason_code": "NO_VERIFIED_PUBLIC_PATTERN"}
    if evidence_class != "CONSISTENT":
        return {"state": "RECHECK", "reason_code": "PUBLIC_EVIDENCE_INCONCLUSIVE"}
    return {"state": "OBSERVED_PATTERN", "reason_code": "I03_CLOSED_CANDLE_CONSENSUS"}


def research_recheck_policy(*, source_current: bool, evidence_valid: bool) -> str:
    """An evaluation policy, not a moment for opening a contract."""
    if not source_current:
        return "RELOAD_PUBLIC_SOURCE"
    if not evidence_valid:
        return "REVALIDATE_PUBLIC_DATA"
    return "AFTER_NEXT_CLOSED_CANDLE"


def research_evidence_checklist(asset: dict, freshness: dict) -> dict:
    """Document objective public-data prerequisites for a research review.

    This checklist is not an order entry detector; a passing dataset does
    not imply a profitable trade or a validated strategy.
    """
    ctx = asset.get("context_timeframe")
    ctx = ctx if isinstance(ctx, dict) else {}
    profile = ctx.get("profile")
    profile = profile if isinstance(profile, dict) else {}
    seconds = tuple(profile.get(key) for key in (
        "higher_seconds", "intermediate_seconds", "lower_seconds",
    ))
    profile_valid = (
        all(type(value) is int and value > 0 for value in seconds)
        and seconds[0] > seconds[1] > seconds[2]
    )
    verdict = ctx.get("alignment")
    verdict = verdict if isinstance(verdict, dict) else {}
    rules = {
        "i01_complete_closed_candles": (
            asset.get("candle_quality") == "COMPLETE"
            and type(asset.get("closed_candles")) is int
            and asset["closed_candles"] >= 2
        ),
        "i02_recent_closed_candle": freshness.get("status") == "CURRENT",
        "i02_research_strategy_identified": bool(
            isinstance(asset.get("strategy_family"), str)
            and asset["strategy_family"].strip()
        ),
        "i03_hierarchical_timeframes_verified": profile_valid,
        "i03_context_has_no_strong_conflict": verdict.get("state") not in (
            "BLOCKED_STRONG_CONFLICT", "INDETERMINATE",
        ) and verdict.get("state") in (_RESEARCH_ALIGNMENTS | _RECHECK_ALIGNMENTS),
    }
    return {
        "version": VERSION,
        "checklist": rules,
        "satisfied_count": sum(value is True for value in rules.values()),
        "total_count": len(rules),
        "timeframe_profile_seconds": (
            dict(zip(("higher", "intermediate", "lower"), seconds))
            if profile_valid else None
        ),
        "completion_kind": "DATA_VALIDATION_NOT_WIN_PROBABILITY",
    }


def research_data_confidence(checklist: dict, *, research_usable: bool) -> dict:
    """A bounded, reproducible completeness measure, not a trade signal.

    A missing prerequisite prevents review readiness even when other public
    indicators happen to align. The score only describes checked metadata.
    """
    rules = checklist.get("checklist") if isinstance(checklist, dict) else None
    if not isinstance(rules, dict) or len(rules) != 5:
        return {
            "points": 0, "grade": "INVALID",
            "kind": "DATA_COMPLETENESS_NOT_PROBABILITY",
        }
    satisfied = sum(value is True for value in rules.values())
    points = 20 * satisfied if research_usable else 0
    return {
        "points": points,
        "grade": ("COMPLETE" if points == 100
                  else "PARTIAL" if points > 0 else "INVALID"),
        "kind": "DATA_COMPLETENESS_NOT_PROBABILITY",
    }


def future_research_adapters() -> dict:
    """Declare unavailable integration boundaries without fabricated contracts."""
    return {
        "knowledge_base": {
            "status": "NOT_CONNECTED", "used": False,
            "interface_version": None, "evidence_count": 0,
        },
        "strategy_intelligence": {
            "status": "I02_PUBLIC_STRATEGY_METADATA_ONLY",
            "used": False, "interface_version": None,
        },
        "distribution": {
            "status": "NOT_CONNECTED", "used": False,
            "interface_version": None, "publish_allowed": False,
        },
    }


def research_lifecycle(
    *, previous_record: dict | None, fingerprint: str,
    source_current: bool, evidence_valid: bool,
) -> dict:
    """Lifecycle of a research record, not the lifecycle of a broker order."""
    prev_fingerprint = (
        previous_record.get("fingerprint") if isinstance(previous_record, dict)
        else None
    )
    duplicate = prev_fingerprint == fingerprint and prev_fingerprint is not None
    change = "UNCHANGED" if duplicate else ("UPDATED" if prev_fingerprint else "NEW")
    if not source_current:
        state, reason = "EXPIRED", "PUBLIC_SOURCE_OUTSIDE_TTL"
    elif not evidence_valid:
        state, reason = "CANCELLED", "PUBLIC_EVIDENCE_INVALID"
    else:
        state, reason = "ACTIVE_RESEARCH", "PUBLIC_OBSERVATION_TRACKED"
    return {
        "lifecycle": change,
        "lifecycle_state": state,
        "lifecycle_reason_code": reason,
        "duplicate_of_previous": duplicate,
    }


def classify_evidence_strength(asset: dict, *, evidence_valid: bool,
                               evidence_class: str) -> str:
    """A qualitative label; never a calibrated probability or trade ranking."""
    if not evidence_valid:
        return "INVALID"
    if evidence_class != "CONSISTENT":
        return "INCONCLUSIVE"
    contexts = asset.get("context_timeframe", {}).get("contexts", {})
    if not isinstance(contexts, dict):
        return "INCONCLUSIVE"
    if all(
        isinstance(contexts.get(role), dict)
        and contexts[role].get("research_usable") is not False
        for role in _ROLES
    ):
        return "CONSISTENT"
    return "INCONCLUSIVE"


def assess_research_report(
    i03_report: dict, *, previous: dict | None = None, now: int | None = None,
) -> dict:
    """Produce bounded, user-agnostic, fail-closed research observations.

    Both WAIT and DO NOT TRADE are observational, not actionable broker
    instructions. There is deliberately NO SIGNAL APPROVED, CALL/PUT, contract
    expiry, or exact entry timing. Trading permissions can never be inferred
    from public data quality, alignment, or any score.
    """
    if not isinstance(i03_report, dict) or not isinstance(i03_report.get("markets"), dict):
        raise ValueError("I06: invalid I03 public research input")
    if now is None:
        now = int(time.time())
    if type(now) is not int or now <= 0:
        raise ValueError("I06: invalid observation clock")
    observed = i03_report.get("observed_at_epoch")
    source_valid = (
        i03_report.get("version") == SOURCE_VERSION
        and i03_report.get("i02_source_version") == I02_VERSION
        and i03_report.get("execution_allowed") is False
        and i03_report.get("approved_for_trading") is False
        and i03_report.get("orders_sent") == 0
        and i03_report.get("confidence_kind") == HEURISTIC
        and type(observed) is int and 0 < observed <= now
    )
    valid_until = observed + RESEARCH_TTL_SECONDS if type(observed) is int and observed > 0 else 0
    current = source_valid and now < valid_until
    prior = _previous_items(previous)
    seen: set[tuple[str, str]] = set()
    records = []
    deferred_count = 0
    for market, section in sorted(i03_report["markets"].items()):
        if not isinstance(market, str) or not isinstance(section, dict):
            continue
        selected = section.get("selected")
        if not isinstance(selected, list):
            continue
        for asset in selected:
            if len(records) >= MAX_ITEMS:
                deferred_count += 1
                continue
            if not isinstance(asset, dict) or not isinstance(asset.get("symbol"), str):
                continue
            symbol = asset["symbol"]
            if not _VALID_SYMBOL.fullmatch(symbol):
                continue
            key = (market, symbol)
            if key in seen:
                # A duplicated source row is rejected, never ranked twice.
                continue
            seen.add(key)
            fingerprint = _fingerprint(i03_report, market, asset)
            previous_record = prior.get(key)
            evidence_valid, evidence_class, reason_codes = _evidence(asset)
            freshness = review_public_freshness(
                asset, observed_at=observed if type(observed) is int else 0,
                now=now,
            )
            if freshness["status"] != "CURRENT":
                evidence_valid = False
                evidence_class = "INVALID"
                reason_codes = reason_codes + [freshness["reason_code"]]
            if not source_valid:
                reason_codes = ["I03_SOURCE_VERSION_OR_GUARD_INVALID"]
            elif not current:
                reason_codes = ["PUBLIC_OBSERVATION_EXPIRED"]
            outcome = "WAIT" if current and evidence_valid else "DO NOT TRADE"
            lifecycle = research_lifecycle(
                previous_record=previous_record, fingerprint=fingerprint,
                source_current=current, evidence_valid=evidence_valid,
            )
            prerequisites = research_evidence_checklist(asset, freshness)
            data_confidence = research_data_confidence(
                prerequisites, research_usable=bool(current and evidence_valid),
            )
            adapter_status = future_research_adapters()
            records.append({
                "symbol": symbol,
                "market": market,
                "fingerprint": fingerprint,
                "observed_at_epoch": observed if type(observed) is int else None,
                "valid_until_epoch": valid_until,
                **lifecycle,
                "decision": outcome,
                "public_context": describe_public_context(asset),
                "freshness": freshness,
                "evidence_strength": classify_evidence_strength(
                    asset, evidence_valid=evidence_valid,
                    evidence_class=evidence_class,
                ),
                "pattern_detection": detect_public_research_pattern(
                    asset, evidence_valid=evidence_valid,
                    evidence_class=evidence_class,
                ),
                "recheck_policy": research_recheck_policy(
                    source_current=current, evidence_valid=evidence_valid,
                ),
                "research_prerequisites": prerequisites,
                "research_confidence": data_confidence,
                "research_review": (
                    "DATA_READY_FOR_STUDY"
                    if data_confidence["points"] == 100 and current
                    and evidence_valid else "RECHECK_OR_REJECT"
                ),
                "validity_window": {
                    "from_epoch": observed if type(observed) is int else None,
                    "until_epoch": valid_until,
                    "kind": "PUBLIC_DATA_RECHECK_ONLY",
                    "is_current": bool(current),
                },
                "strategy_provenance": {
                    "family": asset.get("strategy_family"),
                    "strategy_version": None,
                    "i02_source_version": I02_VERSION,
                    "status": "UNVERSIONED_PUBLIC_METADATA",
                },
                "knowledge_base": adapter_status["knowledge_base"],
                "strategy_intelligence": adapter_status["strategy_intelligence"],
                "distribution": adapter_status["distribution"],
                "evidence_class": evidence_class if current and source_valid else "INVALID",
                "reason_codes": sorted(set(reason_codes + ["TRADE_ACTIONS_NOT_SUPPORTED"])),
                "evidence_kind": HEURISTIC,
                "research_usable": bool(current and evidence_valid),
                "trade_side": None,
                "contract_expiry_minutes": None,
                "entry_epoch": None,
                "approved_for_trading": False,
                "execution_allowed": False,
            })
    records.sort(key=lambda row: (row["market"], row["symbol"]))
    expired = [
        {"market": market, "symbol": symbol, "lifecycle": "RETIRED",
         "lifecycle_state": "CONCLUDED",
         "reason_codes": ["NOT_PRESENT_IN_CURRENT_PUBLIC_SCAN"]}
        for (market, symbol) in sorted(prior)
        if (market, symbol) not in seen
    ][:MAX_ITEMS]
    return {
        "version": VERSION,
        "source_version": i03_report.get("version"),
        "i02_source_version": i03_report.get("i02_source_version"),
        "observed_at_epoch": observed if type(observed) is int else None,
        "valid_until_epoch": valid_until,
        "report_state": "CURRENT_RESEARCH" if current else "UNTRUSTED_OR_EXPIRED_SOURCE",
        "research_records": records,
        "retired_records": expired,
        "not_evaluated_capacity": deferred_count,
        "source_scan_coverage_complete": i03_report.get("scan_coverage_complete") is True,
        "source_context_coverage_complete": i03_report.get("context_coverage_complete") is True,
        "trade_signal_approval_supported": False,
        "signal_outcomes_supported": ["WAIT", "DO NOT TRADE"],
        "research_review_outcomes_supported": [
            "DATA_READY_FOR_STUDY", "RECHECK_OR_REJECT",
        ],
        "distribution": future_research_adapters()["distribution"],
        "score_kind": HEURISTIC,
        "orders_sent": 0,
        "approved_for_trading": False,
        "execution_allowed": False,
    }

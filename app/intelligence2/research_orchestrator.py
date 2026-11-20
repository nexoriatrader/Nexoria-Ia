"""I01/I02/I03/I06 public research report orchestrator, strictly non-executable.

I01-I03 already feed the I06 facade. This module ONLY coordinates that
existing facade and consolidates its output; it neither re-runs indicators
nor creates scanners, per-user state, persistence, network clients or caches.
All public evidence stays separate from financial authorization and orders.
"""
from __future__ import annotations

import re
import time
from collections import Counter

from .research_signal_engine import (
    HEURISTIC,
    I02_VERSION,
    MAX_ITEMS,
    SOURCE_VERSION,
    VERSION as I06_VERSION,
)

VERSION = "intelligence2_research_orchestrator_v1"
_SYMBOL = re.compile(r"[A-Za-z0-9_]{2,30}\Z")
_MARKET = re.compile(r"[a-z][a-z0-9_]{1,31}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_REASON = re.compile(r"[A-Z0-9_]{1,80}\Z")
_STATES = ("RESEARCH_READY", "WAIT_RECHECK", "EXCLUDED")
_VALID_LIFECYCLE = frozenset(("NEW", "UPDATED", "UNCHANGED"))
_VALID_TREND = frozenset(("UP", "DOWN", "NEUTRAL"))


def _reasons(raw: object, *extra: str) -> list[str]:
    source = raw if isinstance(raw, list) else []
    return sorted(set(
        s for s in [*source[:32], *extra]
        if isinstance(s, str) and _REASON.fullmatch(s)
    ))


def _distribution_boundary() -> dict:
    return {
        "status": "NOT_CONNECTED",
        "publish_allowed": False,
        "execution_allowed": False,
    }


def _empty_report(*, now: int, reason: str, source: dict | None = None) -> dict:
    return {
        "version": VERSION,
        "state": "SOURCE_UNTRUSTED",
        "as_of_epoch": now,
        "source_versions": {
            "i02": (source or {}).get("i02_source_version"),
            "i03": (source or {}).get("source_version"),
            "i06": (source or {}).get("version"),
        },
        "assets": [],
        "counts": {name: 0 for name in _STATES},
        "asset_count": 0,
        "reason_codes": [reason],
        "integrations": {"i04": "NOT_CONNECTED", "i05": "NOT_CONNECTED"},
        "distribution": _distribution_boundary(),
        "shared_public_research": True,
        "confidence_kind": "DATA_COMPLETENESS_NOT_PROBABILITY",
        "orders_sent": 0,
        "approved_for_trading": False,
        "execution_allowed": False,
    }


def _trusted_source(source: dict) -> bool:
    if any(key in source for key in (
        "account_id", "user_id", "session", "token", "balance",
        "preserved_open_contracts",
    )):
        return False
    distribution = source.get("distribution")
    return (
        source.get("version") == I06_VERSION
        and source.get("source_version") == SOURCE_VERSION
        and source.get("i02_source_version") == I02_VERSION
        and source.get("orders_sent") == 0
        and source.get("approved_for_trading") is False
        and source.get("execution_allowed") is False
        and source.get("score_kind") == HEURISTIC
        and source.get("trade_signal_approval_supported") is False
        and isinstance(distribution, dict)
        and distribution.get("publish_allowed") is False
        and isinstance(source.get("research_records"), list)
        and len(source["research_records"]) <= MAX_ITEMS
        and source.get("report_state") in (
            "CURRENT_RESEARCH", "UNTRUSTED_OR_EXPIRED_SOURCE",
        )
        and type(source.get("observed_at_epoch")) is int
        and type(source.get("valid_until_epoch")) is int
        and 0 < source["observed_at_epoch"] < source["valid_until_epoch"]
    )


def _safe_record(record: dict, *, observed: int, now: int,
                 source_valid: bool, repeated: bool) -> dict | None:
    market, symbol = record.get("market"), record.get("symbol")
    if (
        not isinstance(market, str) or not _MARKET.fullmatch(market)
        or not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
    ):
        return None
    fingerprint = record.get("fingerprint")
    identity_ok = isinstance(fingerprint, str) and bool(_SHA256.fullmatch(fingerprint))
    forbidden_keys = ("account_id", "user_id", "session", "token",
                      "balance", "preserved_open_contracts")
    private = any(key in record for key in forbidden_keys)
    distribution = record.get("distribution")
    non_executable = (
        record.get("execution_allowed") is False
        and record.get("approved_for_trading") is False
        and record.get("trade_side") is None
        and record.get("entry_epoch") is None
        and record.get("contract_expiry_minutes") is None
        and isinstance(distribution, dict)
        and distribution.get("publish_allowed") is False
    )
    clock_valid = (
        type(record.get("observed_at_epoch")) is int
        and record["observed_at_epoch"] == observed
        and type(record.get("valid_until_epoch")) is int
        and observed < record["valid_until_epoch"]
    )
    current = source_valid and clock_valid and now < record["valid_until_epoch"]
    freshness = record.get("freshness")
    freshness_valid = isinstance(freshness, dict) and freshness.get("status") == "CURRENT"
    ctx = record.get("public_context")
    trend = ctx.get("observed_trend") if isinstance(ctx, dict) else None
    consensus = ctx.get("consensus") if isinstance(ctx, dict) else None
    if trend not in _VALID_TREND:
        trend = "NEUTRAL"
    if consensus not in ("THREE_TIMEFRAMES", "MIXED_OR_PENDING", "NO_DIRECTION", "UNAVAILABLE"):
        consensus = "UNAVAILABLE"
    prerequisites = record.get("research_prerequisites")
    rules = prerequisites.get("checklist") if isinstance(prerequisites, dict) else None
    satisfied = (
        isinstance(rules, dict) and len(rules) == 5
        and all(value is True for value in rules.values())
    )
    score = record.get("research_confidence")
    score_valid = (
        isinstance(score, dict)
        and score.get("kind") == "DATA_COMPLETENESS_NOT_PROBABILITY"
        and type(score.get("points")) is int
        and score["points"] in (0, 20, 40, 60, 80, 100)
    )
    points = score["points"] if score_valid and non_executable else 0
    quality = record.get("evidence_class")
    pattern = record.get("pattern_detection")
    observed_pattern = isinstance(pattern, dict) and pattern.get("state") == "OBSERVED_PATTERN"
    lifecycle = record.get("lifecycle")
    lifecycle_state = record.get("lifecycle_state")
    guard_ok = (
        not private and non_executable and identity_ok and clock_valid
        and score_valid and lifecycle in _VALID_LIFECYCLE
        and lifecycle_state in ("ACTIVE_RESEARCH", "CANCELLED", "EXPIRED")
        and record.get("decision") in ("WAIT", "DO NOT TRADE")
    )
    eligible = (
        guard_ok and current and freshness_valid and satisfied and points == 100
        and quality == "CONSISTENT" and record.get("research_review") == "DATA_READY_FOR_STUDY"
        and lifecycle_state == "ACTIVE_RESEARCH"
        and record.get("decision") == "WAIT"
        and observed_pattern and consensus == "THREE_TIMEFRAMES"
        and trend in ("UP", "DOWN")
        and not record.get("duplicate_of_previous")
        and not repeated
    )
    reason = []
    if not guard_ok:
        state = "EXCLUDED"
        reason.append("I06_RECORD_INTEGRITY_INVALID")
    elif not current:
        state = "EXCLUDED"
        reason.append("OBSERVATION_EXPIRED_OR_UNTRUSTED")
    elif repeated:
        state = "EXCLUDED"
        reason.append("DUPLICATED_ASSET_IN_SOURCE")
    elif record["decision"] == "DO NOT TRADE" or lifecycle_state != "ACTIVE_RESEARCH":
        state = "EXCLUDED"
        reason.append("I06_EVIDENCE_EXCLUDED")
    elif eligible:
        state = "RESEARCH_READY"
        reason.append("PUBLIC_DATA_READY_FOR_STUDY")
    else:
        state = "WAIT_RECHECK"
        reason.append("PUBLIC_EVIDENCE_REQUIRES_RECHECK")
        if record.get("duplicate_of_previous") or lifecycle == "UNCHANGED":
            reason.append("UNCHANGED_PUBLIC_OBSERVATION")
        if not satisfied:
            reason.append("RESEARCH_PREREQUISITES_INCOMPLETE")
        if not freshness_valid:
            reason.append("CLOSED_CANDLE_FRESHNESS_UNVERIFIED")
        if consensus != "THREE_TIMEFRAMES":
            reason.append("CONTEXT_CONSENSUS_INSUFFICIENT")
    if state != "RESEARCH_READY":
        points = points if guard_ok and current else 0
    return {
        "market": market,
        "symbol": symbol,
        "state": state,
        "observed_trend": trend if guard_ok else "NEUTRAL",
        "context_consensus": consensus if guard_ok else "UNAVAILABLE",
        "evidence_strength": record.get("evidence_strength") if guard_ok else "INVALID",
        "evidence_completeness_points": points,
        "confidence_kind": "DATA_COMPLETENESS_NOT_PROBABILITY",
        "fingerprint": fingerprint if identity_ok and not private else None,
        "observed_at_epoch": observed,
        "valid_until_epoch": record.get("valid_until_epoch") if clock_valid else None,
        "lifecycle": lifecycle if lifecycle in _VALID_LIFECYCLE else "INVALID",
        "reason_codes": _reasons(record.get("reason_codes"), *reason),
        "source_modules": ("I01", "I02", "I03", "I06"),
        "distribution": _distribution_boundary(),
        "approved_for_trading": False,
        "execution_allowed": False,
    }


def consolidate_research_report(i06_report: dict, *, now: int | None = None) -> dict:
    """Consolidate bounded I06 records; never recompute evidence or publish."""
    if now is None:
        now = int(time.time())
    if type(now) is not int or now <= 0:
        raise ValueError("Research orchestrator requires a positive clock")
    if not isinstance(i06_report, dict) or not _trusted_source(i06_report):
        return _empty_report(now=now, reason="I06_SOURCE_UNTRUSTED",
                             source=i06_report if isinstance(i06_report, dict) else None)
    observed = i06_report["observed_at_epoch"]
    source_current = (
        i06_report["report_state"] == "CURRENT_RESEARCH"
        and observed <= now < i06_report["valid_until_epoch"]
    )
    duplicates = Counter(
        (x.get("market"), x.get("symbol"))
        for x in i06_report["research_records"]
        if isinstance(x, dict)
        and isinstance(x.get("market"), str)
        and isinstance(x.get("symbol"), str)
    )
    assets = []
    seen = set()
    for raw in i06_report["research_records"]:
        if not isinstance(raw, dict):
            continue
        key = (raw.get("market"), raw.get("symbol"))
        if key in seen:
            continue
        seen.add(key)
        record = _safe_record(
            raw, observed=observed, now=now,
            source_valid=source_current,
            repeated=duplicates[key] > 1,
        )
        if record is not None:
            assets.append(record)
    assets.sort(key=lambda item: (item["market"], item["symbol"]))
    counts = {name: sum(item["state"] == name for item in assets) for name in _STATES}
    state = (
        "SOURCE_EXPIRED" if not source_current else
        "RESEARCH_CANDIDATES" if counts["RESEARCH_READY"] else
        "WAIT_RECHECK" if counts["WAIT_RECHECK"] else "NO_VALID_RESEARCH"
    )
    return {
        "version": VERSION,
        "state": state,
        "as_of_epoch": now,
        "source_versions": {
            "i02": I02_VERSION,
            "i03": SOURCE_VERSION,
            "i06": I06_VERSION,
        },
        "assets": assets,
        "counts": counts,
        "asset_count": len(assets),
        "reason_codes": (
            ["PUBLIC_SOURCE_OUTSIDE_TTL"] if not source_current
            else ["RESEARCH_REPORT_CONSOLIDATED"]
        ),
        "integrations": {"i04": "NOT_CONNECTED", "i05": "NOT_CONNECTED"},
        "distribution": _distribution_boundary(),
        "shared_public_research": True,
        "confidence_kind": "DATA_COMPLETENESS_NOT_PROBABILITY",
        "orders_sent": 0,
        "approved_for_trading": False,
        "execution_allowed": False,
    }


async def orchestrate_public_research(*, previous: dict | None = None,
                                     prior_i06: dict | None = None,
                                     expiry_minutes: int = 5,
                                     now: int | None = None) -> dict:
    """Delegate one scan to the existing I01->I02->I03->I06 facade.

    The caller owns any already existing public cache. This method does not
    add a route, DB table, scheduled loop, websocket or execution side effect.
    """
    from ..asset_scanner import scan_intelligence_research

    if now is None:
        now = int(time.time())
    if type(now) is not int or now <= 0:
        raise ValueError("Research orchestrator requires a positive clock")
    report = await scan_intelligence_research(
        previous=previous, prior_i06=prior_i06,
        expiry_minutes=expiry_minutes, now=now,
    )
    return consolidate_research_report(report, now=now)

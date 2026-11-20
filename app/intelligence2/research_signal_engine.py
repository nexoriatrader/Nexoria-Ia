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
        "quality": asset.get("candle_quality"),
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
            same = previous_record is not None and previous_record.get("fingerprint") == fingerprint
            evidence_valid, evidence_class, reason_codes = _evidence(asset)
            if not source_valid:
                reason_codes = ["I03_SOURCE_VERSION_OR_GUARD_INVALID"]
            elif not current:
                reason_codes = ["PUBLIC_OBSERVATION_EXPIRED"]
            if not current or not evidence_valid:
                outcome = "DO NOT TRADE"
            else:
                outcome = "WAIT"
            previous_version = previous_record.get("fingerprint") if previous_record else None
            records.append({
                "symbol": symbol,
                "market": market,
                "fingerprint": fingerprint,
                "observed_at_epoch": observed if type(observed) is int else None,
                "valid_until_epoch": valid_until,
                "lifecycle": "UNCHANGED" if same else ("UPDATED" if previous_version else "NEW"),
                "duplicate_of_previous": bool(same),
                "decision": outcome,
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
        "score_kind": HEURISTIC,
        "orders_sent": 0,
        "approved_for_trading": False,
        "execution_allowed": False,
    }

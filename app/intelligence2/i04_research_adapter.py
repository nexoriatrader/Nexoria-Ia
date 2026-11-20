"""Read-only I04 bridge for the stacked I01/I02/I03/I06 research orchestrator.

Consumes the existing I04 branch contract: enabled(), retrieve_active(),
get_knowledge(). Does not create or copy I04's database, implement CRUD,
mutate knowledge states, call its maintenance routines, cache private data,
or authorize orders. If the I04 module/schema is absent or disabled,
knowledge has no effect on the public research assessment.
"""
from __future__ import annotations

from datetime import datetime, timezone
from importlib import import_module
import re

from .research_orchestrator import VERSION as ORCHESTRATOR_VERSION

VERSION = "intelligence2_i04_research_adapter_v1"
I04_VERSION = "intelligence2_knowledge_base_v1"
MAX_ASSETS = 8
MAX_RECORDS_PER_CATEGORY = 8
MAX_DETAILS_PER_CATEGORY = 4

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9_]{2,60}\Z")
_SAFE_REASON = re.compile(r"[A-Z][A-Z0-9_]{2,79}\Z")
_CATEGORIES = ("MARKET", "STRATEGY")
_REGIME_FOR_TREND = {"UP": "UPTREND", "DOWN": "DOWNTREND"}
_FINAL_STATES = ("RESEARCH_READY", "WAIT_RECHECK", "EXCLUDED")


def _utc(value: str) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _reason(code: object) -> str:
    return code if isinstance(code, str) and _SAFE_REASON.fullmatch(code) else "I04_UNVERIFIED_REASON"


def _context(asset: dict) -> tuple[str | None, tuple[int, ...], str | None]:
    regime = _REGIME_FOR_TREND.get(asset.get("observed_trend"))
    if asset.get("context_consensus") != "THREE_TIMEFRAMES":
        regime = None
    profile = asset.get("research_timeframes_seconds")
    if not isinstance(profile, dict):
        return regime, (), None
    if set(profile) != {"higher", "intermediate", "lower"}:
        return regime, (), None
    values = tuple(profile[role] for role in ("higher", "intermediate", "lower"))
    if not all(type(v) is int and 0 < v <= 86_400 for v in values):
        return regime, (), None
    if not (values[0] > values[1] > values[2]):
        return regime, (), None
    family = asset.get("strategy_family")
    family = family if isinstance(family, str) and _NAME.fullmatch(family) else None
    return regime, values, family


def _lineage_usable(item: dict, detail: dict, *, category: str,
                    market: str, symbol: str, regime: str,
                    timeframes: tuple[int, ...], family: str | None,
                    now: datetime) -> tuple[str, dict | None]:
    """Recheck fresh I04 state, revisions, clock and existing decision lineage.

    The I04 implementation remains authoritative for digest/CAS and evidence
    sufficiency; this adapter only checks its public read contract and does
    not reproduce its mutation or validation pipeline.
    """
    if any(k in detail for k in ("user_id", "account_id", "balance", "token", "stake", "pnl")):
        return "PRIVATE_KNOWLEDGE_REFUSED", None
    if not isinstance(item, dict) or not isinstance(detail, dict):
        return "I04_RECORD_MALFORMED", None
    if any(k in item for k in ("user_id", "account_id", "balance", "token", "stake", "pnl")):
        return "PRIVATE_KNOWLEDGE_REFUSED", None
    knowledge_id, revision = item.get("id"), item.get("revision")
    if (
        type(knowledge_id) is not int or knowledge_id <= 0
        or type(revision) is not int or revision <= 1
        or detail.get("id") != knowledge_id or detail.get("revision") != revision
        or item.get("version") != I04_VERSION
        or detail.get("version") != I04_VERSION
        or item.get("state") != "ACTIVE" or detail.get("state") != "ACTIVE"
        or item.get("reusable_for_research") is not True
        or detail.get("reusable_for_research") is not True
        or item.get("execution_allowed") is not False
        or detail.get("execution_allowed") is not False
        or item.get("approved_for_trading") is not False
        or detail.get("approved_for_trading") is not False
        or item.get("category") != category or detail.get("category") != category
        or item.get("market") != market or detail.get("market") != market
        or item.get("symbol") != symbol or detail.get("symbol") != symbol
        or item.get("regime") not in ("UNKNOWN", regime)
        or detail.get("regime") not in ("UNKNOWN", regime)
        or item.get("reuse_status") != "CURRENT_VERIFIED_ACTIVE"
        or detail.get("reuse_status") != "CURRENT_VERIFIED_ACTIVE"
    ):
        return "I04_STATE_OR_IDENTITY_MISMATCH", None
    tf = detail.get("timeframe_seconds")
    if type(tf) is not int or tf not in (0, *timeframes):
        return "I04_TIMEFRAME_NOT_RELEVANT", None
    from_at, until, review = (
        _utc(detail.get(key)) for key in ("valid_from", "valid_until", "next_review_at")
    )
    if (
        None in (from_at, until, review)
        or not from_at <= now < min(until, review)
        or item.get("valid_until") != detail.get("valid_until")
        or item.get("next_review_at") != detail.get("next_review_at")
    ):
        return "I04_REVIEW_REQUIRED_OR_EXPIRED", None
    digest = detail.get("payload_sha256")
    if not isinstance(digest, str) or not _SHA.fullmatch(digest):
        return "I04_DIGEST_UNVERIFIED", None
    payload = detail.get("payload")
    if isinstance(payload, dict) and any(
        k in payload for k in ("user_id", "account_id", "balance", "token", "stake", "pnl")
    ):
        return "PRIVATE_KNOWLEDGE_REFUSED", None
    evidence = detail.get("evidence")
    decisions = detail.get("decisions")
    if (
        not isinstance(payload, dict)
        or not isinstance(evidence, list) or not isinstance(decisions, list)
        or not decisions
        or decisions[-1].get("revision") != revision
        or decisions[-1].get("to_state") != "ACTIVE"
    ):
        return "I04_EVIDENCE_OR_HISTORY_UNAVAILABLE", None
    # I04 already requires independently verified evidence to activate.
    # Require the public lineage returned by get_knowledge to be present,
    # without copying evidence summaries or sensitive fields.
    origins = {
        entry.get("origin_group")
        for entry in evidence
        if isinstance(entry, dict)
        and entry.get("verified") is True
        and type(entry.get("observations")) is int
        and entry["observations"] >= 72
        and isinstance(entry.get("origin_group"), str)
    }
    if len(origins) < 2:
        return "I04_VERIFIED_ORIGINS_INSUFFICIENT", None
    if category == "STRATEGY":
        if family is None or payload.get("strategy_family") != family:
            return "I04_STRATEGY_NOT_RELEVANT", None
    payload_direction = payload.get("direction")
    if payload_direction is not None and payload_direction != (
        "UP" if regime == "UPTREND" else "DOWN"
    ):
        return "I04_PUBLIC_DIRECTION_CONFLICT", None
    return "I04_PUBLIC_EVIDENCE_CORROBORATES", {
        "knowledge_id": knowledge_id,
        "revision": revision,
        "category": category,
        "subject": detail.get("subject") if isinstance(detail.get("subject"), str) else None,
        "model_version": detail.get("model_version") if isinstance(detail.get("model_version"), str) else None,
        "timeframe_seconds": tf,
        "payload_sha256": digest,
        "verified_origin_count": len(origins),
        "valid_until": detail["valid_until"],
        "next_review_at": detail["next_review_at"],
        "contribution": "PUBLIC_CONTEXT_CORROBORATION",
    }


def _knowledge_view(status: str, *, used=None, excluded=None, consulted=0) -> dict:
    return {
        "version": VERSION,
        "status": status,
        "consulted_count": consulted,
        "used_count": len(used or []),
        "used_public_knowledge": used or [],
        "excluded_public_knowledge": excluded or [],
        "influenced_financial_execution": False,
        "independent_trade_approval": False,
    }


def _load_provider(provider: object | None) -> object | None:
    if provider is not None:
        return provider
    try:
        # The I04 branch is not an ancestor of the I06/orchestrator branches.
        # Bind only when its own module has been integrated and enabled.
        return import_module("app.intelligence2.knowledge_base")
    except ImportError:
        return None


def enrich_research_with_i04(report: dict, *, now: int | None = None,
                             provider: object | None = None) -> dict:
    """Attach validated existing I04 evidence to one public orchestrator report.

    Never upgrades RESEARCH_READY or completeness points. A contradictory
    ACTIVE knowledge record may downgrade a research assessment to WAIT.
    I04 lifecycle/revalidation is owned by the existing Knowledge Base, not
    this adapter: no auto-renewal, mutation, cache or second storage.
    """
    if not isinstance(report, dict):
        raise ValueError("Orchestration report must be a public object")
    if now is None:
        now = report.get("as_of_epoch")
    if type(now) is not int or now <= 0:
        raise ValueError("I04 read clock must be a positive epoch")
    if (
        report.get("version") != ORCHESTRATOR_VERSION
        or report.get("execution_allowed") is not False
        or report.get("approved_for_trading") is not False
        or report.get("orders_sent") != 0
        or not isinstance(report.get("assets"), list)
        or len(report["assets"]) > 48
        or not isinstance(report.get("distribution"), dict)
        or report["distribution"].get("publish_allowed") is not False
        or any(k in report for k in ("user_id", "account_id", "balance", "token"))
        or any(
            not isinstance(asset, dict)
            or asset.get("state") not in _FINAL_STATES
            or asset.get("execution_allowed") is not False
            or asset.get("approved_for_trading") is not False
            for asset in report["assets"]
        )
    ):
        raise ValueError("I04 refuses untrusted or financial source")
    module = _load_provider(provider)
    if module is None:
        status = "NOT_CONNECTED"
    else:
        try:
            if getattr(module, "VERSION", None) != I04_VERSION:
                status = "INCOMPATIBLE_I04_CONTRACT"
            elif not callable(getattr(module, "enabled", None)) or not module.enabled():
                status = "DISABLED"
            elif not callable(getattr(module, "retrieve_active", None)) or not callable(getattr(module, "get_knowledge", None)):
                status = "INCOMPATIBLE_I04_CONTRACT"
            else:
                status = "READ_ONLY_CONNECTED"
        except Exception:
            status = "UNAVAILABLE"
    utc = datetime.fromtimestamp(now, tz=timezone.utc)
    assets = []
    touched = 0
    failed = False
    for asset in report["assets"]:
        updated = dict(asset)
        if status != "READ_ONLY_CONNECTED":
            updated["knowledge_base"] = _knowledge_view(status)
            assets.append(updated)
            continue
        if asset["state"] == "EXCLUDED":
            updated["knowledge_base"] = _knowledge_view("SKIPPED_EXCLUDED_RESEARCH")
            assets.append(updated)
            continue
        regime, timeframes, family = _context(asset)
        if regime is None or not timeframes:
            updated["knowledge_base"] = _knowledge_view("I04_CONTEXT_NOT_VERIFIED")
            assets.append(updated)
            continue
        if touched >= MAX_ASSETS:
            updated["knowledge_base"] = _knowledge_view("I04_CAPACITY_DEFERRED")
            if updated["state"] == "RESEARCH_READY":
                updated["state"] = "WAIT_RECHECK"
                updated["reason_codes"] = sorted(set([
                    *updated.get("reason_codes", []), "I04_COVERAGE_DEFERRED",
                ]))
            assets.append(updated)
            continue
        touched += 1
        used, excluded = [], []
        consulted = 0
        conflict = False
        unavailable = False
        for category in _CATEGORIES:
            if category == "STRATEGY" and family is None:
                continue
            try:
                response = module.retrieve_active(
                    category=category, market=asset["market"],
                    symbol=asset["symbol"], observed_regime=regime,
                    now=utc, limit=MAX_RECORDS_PER_CATEGORY,
                )
                if (
                    not isinstance(response, dict)
                    or response.get("version") != I04_VERSION
                    or response.get("category") != category
                    or response.get("market") != asset["market"]
                    or response.get("symbol") != asset["symbol"]
                    or response.get("observed_regime") != regime
                    or response.get("read_only") is not True
                    or response.get("execution_allowed") is not False
                    or not isinstance(response.get("active"), list)
                    or not isinstance(response.get("excluded"), list)
                    or len(response["active"]) > MAX_RECORDS_PER_CATEGORY
                ):
                    raise ValueError("Invalid I04 read contract")
                consulted += 1
                for old in response["excluded"][:MAX_RECORDS_PER_CATEGORY]:
                    if isinstance(old, dict) and type(old.get("id")) is int:
                        excluded.append({
                            "knowledge_id": old["id"], "category": category,
                            "reason": _reason(old.get("reason")),
                        })
                for item in response["active"][:MAX_DETAILS_PER_CATEGORY]:
                    if not isinstance(item, dict) or type(item.get("id")) is not int:
                        excluded.append({"knowledge_id": None, "category": category,
                                         "reason": "I04_ACTIVE_RECORD_MALFORMED"})
                        continue
                    detail = module.get_knowledge(
                        item["id"], now=utc, observed_regime=regime,
                        include_lineage=True,
                    )
                    reason, good = _lineage_usable(
                        item, detail, category=category,
                        market=asset["market"], symbol=asset["symbol"],
                        regime=regime, timeframes=timeframes, family=family,
                        now=utc,
                    )
                    if good is not None:
                        used.append(good)
                    else:
                        excluded.append({"knowledge_id": item["id"],
                                         "category": category, "reason": reason})
                        if reason == "I04_PUBLIC_DIRECTION_CONFLICT":
                            conflict = True
                if len(response["active"]) > MAX_DETAILS_PER_CATEGORY:
                    unavailable = True
                    excluded.append({
                        "knowledge_id": None, "category": category,
                        "reason": "I04_RETRIEVAL_COVERAGE_BOUNDED",
                    })
            except Exception:
                # I04's real integrity/DB errors must never be masked by
                # fallback knowledge. No data from a failed read is used.
                used.clear()
                unavailable = True
                excluded.append({
                    "knowledge_id": None, "category": category,
                    "reason": "I04_READ_OR_INTEGRITY_UNAVAILABLE",
                })
                break
        if unavailable:
            used.clear()
        if conflict or unavailable:
            if updated["state"] == "RESEARCH_READY":
                updated["state"] = "WAIT_RECHECK"
            updated["reason_codes"] = sorted(set([
                *updated.get("reason_codes", []),
                "I04_ACTIVE_CONTEXT_CONFLICT" if conflict else "I04_READ_UNAVAILABLE",
            ]))
        if used and not unavailable and not conflict:
            updated["reason_codes"] = sorted(set([
                *updated.get("reason_codes", []),
                "I04_PUBLIC_KNOWLEDGE_CORROBORATED",
            ]))
        updated["knowledge_base"] = _knowledge_view(
            "I04_UNAVAILABLE" if unavailable else
            "I04_CONFLICT_RECHECK" if conflict else
            "I04_CURRENT_ACTIVE_VERIFIED" if used else "I04_NO_REUSABLE_KNOWLEDGE",
            used=used, excluded=excluded, consulted=consulted,
        )
        assets.append(updated)
        failed = failed or unavailable
    counts = {
        name: sum(asset["state"] == name for asset in assets)
        for name in _FINAL_STATES
    }
    prior_state = report.get("state")
    new_state = (
        prior_state if prior_state in ("SOURCE_EXPIRED", "SOURCE_UNTRUSTED") else
        "RESEARCH_CANDIDATES" if counts["RESEARCH_READY"] else
        "WAIT_RECHECK" if counts["WAIT_RECHECK"] else "NO_VALID_RESEARCH"
    )
    integrations = dict(report.get("integrations") or {})
    integrations["i04"] = (
        "READ_ONLY_PARTIAL" if failed else status
    )
    return {
        **report,
        "state": new_state,
        "assets": assets,
        "counts": counts,
        "integrations": integrations,
        "i04_adapter_version": VERSION,
        "i04_enrichment_status": integrations["i04"],
        "i04_queried_asset_count": touched,
        "distribution": {"status": "NOT_CONNECTED", "publish_allowed": False,
                         "execution_allowed": False},
        "orders_sent": 0,
        "approved_for_trading": False,
        "execution_allowed": False,
    }


async def orchestrate_research_with_i04(*, previous: dict | None = None,
                                        prior_i06: dict | None = None,
                                        expiry_minutes: int = 5,
                                        now: int | None = None,
                                        provider: object | None = None) -> dict:
    """Use existing one-pass research chain, then one bounded I04 read pass."""
    import asyncio
    from .research_orchestrator import orchestrate_public_research

    if now is None:
        from time import time
        now = int(time())
    if type(now) is not int or now <= 0:
        raise ValueError("Invalid orchestration clock")
    base = await orchestrate_public_research(
        previous=previous, prior_i06=prior_i06,
        expiry_minutes=expiry_minutes, now=now,
    )
    # Existing SQLAlchemy SessionLocal ownership stays inside I04 functions.
    return await asyncio.to_thread(
        enrich_research_with_i04, base, now=now, provider=provider,
    )

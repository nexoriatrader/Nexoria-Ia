"""I05 Learning Registry -> public Research Orchestrator: READ-ONLY adapter.

I05 still lives on a separate stacked branch. Consume only its actual
learning_enabled()/get_research_release() contracts and the existing I04
get_knowledge() lineage; do not copy models, train, promote, monitor/rollback,
create persistence, access tenants, or introduce trading-side decisions.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from importlib import import_module
import re
from time import time

from .i04_research_adapter import VERSION as I04_ADAPTER_VERSION
from .research_orchestrator import VERSION as ORCHESTRATOR_VERSION

VERSION = "intelligence2_i05_research_adapter_v1"
I05_VERSION = "intelligence2_learning_regimes_v1"
I05_SOURCE_VERSION = "i05_public_closed_regimes_v1"
I04_VERSION = "intelligence2_knowledge_base_v1"
MAX_ASSETS = 8
MIN_TRAIN = 500
MIN_HOLDOUT = 200
EMBARGO_BARS = 72

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MODEL_VERSION = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_STATES = ("RESEARCH_READY", "WAIT_RECHECK", "EXCLUDED")
_ALLOWED_EVENTS = ("PROMOTED", "MONITORED", "AUTO_ROLLBACK")
_VALID_TF = frozenset((60, 300, 900, 3600))
_GROUPS = ("PUBLIC_TRAIN_WINDOW", "PUBLIC_HOLDOUT_WINDOW",
           "PUBLIC_INDEPENDENT_VALIDATION")
_SENSITIVE = frozenset((
    "account_id", "user_id", "session", "token", "balance",
    "stake", "pnl", "real_orders", "demo_orders", "contracts",
    "mode", "account_type",
))


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _view(status: str, *, release: dict | None = None,
          champion: dict | None = None, fallback: dict | None = None,
          reason: str | None = None, scope: dict | None = None) -> dict:
    return {
        "version": VERSION,
        "status": status,
        "reason_codes": [reason] if reason else [],
        "scope": scope,
        "release_id": release.get("release_id") if release else None,
        "release_generation": release.get("generation") if release else None,
        "release_event": release.get("release_event") if release else None,
        "champion": champion,
        "fallback": fallback,
        "challenger": {"status": "NOT_USED_UNTIL_SEPARATE_VALIDATION"},
        "monitoring": {
            "status": (
                "RESEARCH_AUTO_ROLLBACK_RECORDED"
                if release and release.get("release_event") == "AUTO_ROLLBACK"
                else "NO_REALTIME_DEGRADATION_ASSESSMENT"
            ),
            "fresh_holdout_checked": False,
            "monitor_or_rollback_invoked": False,
        },
        "model_contribution": (
            "PUBLIC_MODEL_PROVENANCE_ONLY" if champion else "NONE"
        ),
        "source_origin": "PUBLIC_CLOSED_REGIME_RESEARCH",
        "financial_source_used": False,
        "automatic_promotion_performed": False,
        "prediction_used_for_trading": False,
        "execution_allowed": False,
        "approved_for_trading": False,
    }


def _providers(registry: object | None,
               knowledge: object | None) -> tuple[object | None, object | None]:
    if registry is None:
        try:
            registry = import_module("app.intelligence2.learning_registry")
        except ImportError:
            registry = None
    if knowledge is None:
        try:
            knowledge = import_module("app.intelligence2.knowledge_base")
        except ImportError:
            knowledge = None
    return registry, knowledge


def _valid_scope(asset: dict) -> dict | None:
    profile = asset.get("research_timeframes_seconds")
    if not isinstance(profile, dict) or set(profile) != {
        "higher", "intermediate", "lower"
    }:
        return None
    values = tuple(profile[role] for role in (
        "higher", "intermediate", "lower"
    ))
    if (
        not all(type(value) is int and value in _VALID_TF for value in values)
        or not (values[0] > values[1] > values[2])
    ):
        return None
    market, symbol = asset.get("market"), asset.get("symbol")
    if (
        not isinstance(market, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", market)
        or not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9_]{2,30}", symbol)
        or asset.get("observed_trend") not in ("UP", "DOWN")
        or asset.get("context_consensus") != "THREE_TIMEFRAMES"
    ):
        return None
    # The lowest verified I03 timeframe is an existing I05 public scope;
    # this is NOT a financial contract duration or signal expiry.
    return {
        "market": market, "symbol": symbol,
        "timeframe_seconds": values[2],
        "observed_public_regime": (
            "UPTREND" if asset["observed_trend"] == "UP" else "DOWNTREND"
        ),
    }


def _validate_release(release: object) -> str:
    if not isinstance(release, dict) or _SENSITIVE.intersection(release):
        return "I05_RELEASE_INVALID"
    if (
        release.get("version") != I05_VERSION
        or release.get("execution_allowed") is not False
        or release.get("approved_for_trading") is not False
    ):
        return "I05_RELEASE_INVALID"
    state = release.get("state")
    if state == "NO_VALIDATED_RESEARCH_MODEL":
        if (
            release.get("champion_knowledge_id") is None
            and release.get("fallback_knowledge_id") is None
        ):
            return "I05_NO_VALIDATED_RESEARCH_MODEL"
        return "I05_RELEASE_INVALID"
    if (
        state != "RESEARCH_CHAMPION"
        or type(release.get("release_id")) is not int
        or release["release_id"] <= 0
        or type(release.get("generation")) is not int
        or release["generation"] < 1
        or type(release.get("champion_knowledge_id")) is not int
        or release["champion_knowledge_id"] <= 0
        or release.get("release_event") not in _ALLOWED_EVENTS
        or (release.get("fallback_knowledge_id") is not None and (
            type(release.get("fallback_knowledge_id")) is not int
            or release["fallback_knowledge_id"] <= 0
        ))
    ):
        return "I05_RELEASE_INVALID"
    return "I05_RELEASE_CANDIDATE"


def _validated_model(knowledge: object, knowledge_id: int, *,
                     scope: dict, now: datetime) -> tuple[str, dict | None]:
    """Read existing I04 MODEL record and corroborate I05's OOS lineage.

    I04 owns hash, state-machine and decision integrity. This does NOT
    refit models, verify their profitability or invoke their predictors.
    """
    try:
        item = knowledge.get_knowledge(
            knowledge_id, now=now,
            observed_regime=scope["observed_public_regime"],
            include_lineage=True,
        )
    except Exception:
        return "I05_MODEL_READ_OR_INTEGRITY_FAILED", None
    if not isinstance(item, dict) or _SENSITIVE.intersection(item):
        return "I05_MODEL_PRIVATE_OR_MALFORMED", None
    payload = item.get("payload")
    if (
        not isinstance(payload, dict)
        or _SENSITIVE.intersection(payload)
        or item.get("version") != I04_VERSION
        or item.get("id") != knowledge_id
        or item.get("category") != "MODEL"
        or item.get("market") != scope["market"]
        or item.get("symbol") != scope["symbol"]
        or item.get("timeframe_seconds") != scope["timeframe_seconds"]
        or item.get("state") != "ACTIVE"
        or item.get("reusable_for_research") is not True
        or item.get("reuse_status") != "CURRENT_VERIFIED_ACTIVE"
        or item.get("approved_for_trading") is not False
        or item.get("execution_allowed") is not False
        or item.get("regime") not in (
            "UNKNOWN", scope["observed_public_regime"],
        )
    ):
        return "I05_MODEL_NOT_ACTIVE_OR_SCOPE_MISMATCH", None
    model_id = payload.get("model_id")
    model_version = item.get("model_version")
    if (
        not isinstance(model_id, str) or not _DIGEST.fullmatch(model_id)
        or not isinstance(model_version, str)
        or not _MODEL_VERSION.fullmatch(model_version)
        or payload.get("source_version") != I05_SOURCE_VERSION
        or type(payload.get("sample_size")) is not int
        or payload["sample_size"] < MIN_TRAIN
        or type(payload.get("holdout_samples")) is not int
        or payload["holdout_samples"] < MIN_HOLDOUT
        or not isinstance(item.get("payload_sha256"), str)
        or not _DIGEST.fullmatch(item["payload_sha256"])
        or type(item.get("revision")) is not int or item["revision"] < 2
    ):
        return "I05_MODEL_VERSION_OR_EVIDENCE_INVALID", None
    start, until, review = (
        _timestamp(item.get(field))
        for field in ("valid_from", "valid_until", "next_review_at")
    )
    if (
        None in (start, until, review)
        or not start <= now < min(until, review)
    ):
        return "I05_MODEL_REVIEW_DUE_OR_EXPIRED", None
    evidence, decisions = item.get("evidence"), item.get("decisions")
    if (
        not isinstance(evidence, list) or not isinstance(decisions, list)
        or not decisions or not isinstance(decisions[-1], dict)
        or decisions[-1].get("revision") != item["revision"]
        or decisions[-1].get("to_state") != "ACTIVE"
        or decisions[-1].get("decision") != "I05_RESEARCH_ONLY_PROMOTION"
    ):
        return "I05_PROMOTION_LINEAGE_INVALID", None
    by_group = {}
    for row in evidence:
        if not isinstance(row, dict):
            continue
        group = row.get("origin_group")
        if group not in _GROUPS or group in by_group:
            continue
        if (
            row.get("verified") is not True
            or row.get("source_type") != "MODEL_ARTIFACT"
            or type(row.get("observations")) is not int
            or row["observations"] < (
                MIN_TRAIN if group == _GROUPS[0] else MIN_HOLDOUT
            )
            or not isinstance(row.get("source_sha256"), str)
            or not _DIGEST.fullmatch(row["source_sha256"])
        ):
            continue
        lower, upper = (
            _timestamp(row.get("period_from")),
            _timestamp(row.get("period_to")),
        )
        if lower is None or upper is None or not lower <= upper <= now:
            continue
        by_group[group] = {
            "from": lower, "until": upper,
            "observation_count": row["observations"],
        }
    if any(group not in by_group for group in _GROUPS):
        return "I05_INDEPENDENT_OOS_VALIDATION_MISSING", None
    train, holdout, second = (by_group[k] for k in _GROUPS)
    tf = scope["timeframe_seconds"]
    if (
        not (train["until"] + timedelta(seconds=EMBARGO_BARS * tf)
             < holdout["from"])
        or not (holdout["until"] + __import__("datetime").timedelta(seconds=EMBARGO_BARS * tf)
                < second["from"])
    ):
        return "I05_OOS_PERIODS_OVERLAP", None
    return "I05_MODEL_VALIDATED_PUBLIC", {
        "knowledge_id": knowledge_id,
        "knowledge_revision": item["revision"],
        "model_version": model_version,
        "model_id_sha256": model_id,
        "knowledge_payload_sha256": item["payload_sha256"],
        "regime_scope": item["regime"],
        "observed_public_regime": scope["observed_public_regime"],
        "validation_periods": {
            name: {
                "from": by_group[name]["from"].isoformat(),
                "until": by_group[name]["until"].isoformat(),
                "observations": by_group[name]["observation_count"],
            }
            for name in _GROUPS
        },
        "valid_until": item["valid_until"],
        "next_review_at": item["next_review_at"],
        "evidence_class": "INDEPENDENT_PUBLIC_REGIME_VALIDATION",
        "financial_origin_used": False,
    }


def enrich_research_with_i05(report: dict, *, now: int | None = None,
                             registry: object | None = None,
                             knowledge: object | None = None) -> dict:
    """Annotate I04-enriched research with verified I05 PUBLIC metadata only.

    No score improvement, candidate promotion, model inference, financial
    decisions, model training, rollback or new storage. Read-only only.
    """
    if not isinstance(report, dict):
        raise ValueError("I05 requires a public research report")
    if now is None:
        now = report.get("as_of_epoch")
    if type(now) is not int or now <= 0:
        raise ValueError("I05 requires a positive public research clock")
    if (
        report.get("version") != ORCHESTRATOR_VERSION
        or report.get("i04_adapter_version") != I04_ADAPTER_VERSION
        or report.get("orders_sent") != 0
        or report.get("execution_allowed") is not False
        or report.get("approved_for_trading") is not False
        or not isinstance(report.get("distribution"), dict)
        or report["distribution"].get("publish_allowed") is not False
        or not isinstance(report.get("assets"), list)
        or len(report["assets"]) > 48
        or _SENSITIVE.intersection(report)
        or any(
            not isinstance(asset, dict)
            or asset.get("state") not in _STATES
            or asset.get("execution_allowed") is not False
            or asset.get("approved_for_trading") is not False
            or _SENSITIVE.intersection(asset)
            for asset in report["assets"]
        )
    ):
        raise ValueError("I05 refuses unsafe or foreign orchestrator output")
    registry, knowledge = _providers(registry, knowledge)
    i04_status = (report.get("integrations") or {}).get("i04")
    if i04_status != "READ_ONLY_CONNECTED":
        status = "BLOCKED_I04_NOT_CONNECTED"
    elif registry is None or knowledge is None:
        status = "NOT_CONNECTED"
    else:
        try:
            if (
                getattr(registry, "VERSION", None) != I05_VERSION
                or getattr(knowledge, "VERSION", None) != I04_VERSION
                or not callable(getattr(registry, "learning_enabled", None))
                or not callable(getattr(registry, "get_research_release", None))
                or not callable(getattr(knowledge, "get_knowledge", None))
            ):
                status = "INCOMPATIBLE_I05_OR_I04_CONTRACT"
            elif registry.learning_enabled() is not True:
                status = "DISABLED"
            else:
                status = "READ_ONLY_CONNECTED"
        except Exception:
            status = "I05_UNAVAILABLE"
    now_utc = datetime.fromtimestamp(now, tz=timezone.utc)
    assets, queried, failures = [], 0, 0
    for asset in report["assets"]:
        copied = dict(asset)
        if status != "READ_ONLY_CONNECTED":
            copied["learning_engine"] = _view(status)
            assets.append(copied)
            continue
        if asset["state"] == "EXCLUDED":
            copied["learning_engine"] = _view("SKIPPED_EXCLUDED_RESEARCH")
            assets.append(copied)
            continue
        if (
            not isinstance(asset.get("knowledge_base"), dict)
            or asset["knowledge_base"].get("status") in (
                "I04_UNAVAILABLE", "SKIPPED_EXCLUDED_RESEARCH",
                "I04_CONTEXT_NOT_VERIFIED",
            )
        ):
            copied["learning_engine"] = _view("BLOCKED_I04_CONTEXT_UNVERIFIED")
            assets.append(copied)
            continue
        scope = _valid_scope(asset)
        if scope is None:
            copied["learning_engine"] = _view("I05_PUBLIC_SCOPE_UNVERIFIED")
            assets.append(copied)
            continue
        if queried >= MAX_ASSETS:
            copied["learning_engine"] = _view(
                "I05_CAPACITY_DEFERRED", scope=scope,
            )
            if copied["state"] == "RESEARCH_READY":
                copied["state"] = "WAIT_RECHECK"
                copied["reason_codes"] = sorted(set([
                    *copied.get("reason_codes", []),
                    "I05_MODEL_SCOPE_DEFERRED",
                ]))
            assets.append(copied)
            continue
        queried += 1
        try:
            release = registry.get_research_release(
                market=scope["market"], symbol=scope["symbol"],
                timeframe_seconds=scope["timeframe_seconds"], now=now_utc,
            )
            release_state = _validate_release(release)
            if release_state == "I05_NO_VALIDATED_RESEARCH_MODEL":
                copied["learning_engine"] = _view(
                    release_state, scope=scope,
                    reason="NO_APPROVED_PUBLIC_MODEL_RELEASE",
                )
                assets.append(copied)
                continue
            if release_state != "I05_RELEASE_CANDIDATE":
                raise ValueError("I05 invalid or unverified release")
            champion_id = release["champion_knowledge_id"]
            champion_status, champion = _validated_model(
                knowledge, champion_id, scope=scope, now=now_utc,
            )
            fallback_id = release.get("fallback_knowledge_id")
            fallback_status, fallback = (None, None)
            if fallback_id is not None and fallback_id != champion_id:
                fallback_status, fallback = _validated_model(
                    knowledge, fallback_id, scope=scope, now=now_utc,
                )
            if champion is None:
                copied["learning_engine"] = _view(
                    "I05_MODEL_REVIEW_REQUIRED", release=release, scope=scope,
                    reason=champion_status,
                )
            else:
                # I05 get_research_release() already applies its own fallback
                # if the former champion was invalidated. Do not promote.
                fallback_selected = fallback_id == champion_id
                label = (
                    "I05_FALLBACK_VERIFIED" if fallback_selected
                    else "I05_CHAMPION_VERIFIED"
                )
                copied["learning_engine"] = _view(
                    label, release=release, scope=scope,
                    champion=champion, fallback=fallback,
                    reason=(
                        "FALLBACK_SELECTED_BY_I05" if fallback_selected
                        else "CHAMPION_ACTIVE_PUBLIC_RESEARCH"
                    ),
                )
                if fallback_status and fallback is None:
                    copied["learning_engine"]["reason_codes"].append(
                        "I05_FALLBACK_NOT_VALIDATED"
                    )
        except Exception:
            failures += 1
            copied["learning_engine"] = _view(
                "I05_RELEASE_OR_MODEL_UNAVAILABLE", scope=scope,
                reason="I05_INTEGRITY_OR_STORAGE_ERROR",
            )
        assets.append(copied)
    integrations = dict(report.get("integrations") or {})
    integrations["i05"] = (
        "READ_ONLY_PARTIAL" if failures else status
    )
    return {
        **report,
        "assets": assets,
        "integrations": integrations,
        "i05_adapter_version": VERSION,
        "i05_queried_asset_count": queried,
        "i05_read_failure_count": failures,
        "distribution": {
            "status": "NOT_CONNECTED",
            "publish_allowed": False,
            "execution_allowed": False,
        },
        "orders_sent": 0,
        "execution_allowed": False,
        "approved_for_trading": False,
    }


async def orchestrate_research_with_i04_i05(
    *, previous: dict | None = None, prior_i06: dict | None = None,
    expiry_minutes: int = 5, now: int | None = None,
    knowledge_provider: object | None = None,
    registry_provider: object | None = None,
) -> dict:
    """Reuse existing I01..I06 and I04 one-pass façade then read I05 release."""
    from .i04_research_adapter import orchestrate_research_with_i04

    if now is None:
        now = int(time())
    if type(now) is not int or now <= 0:
        raise ValueError("Invalid shared research observation clock")
    source = await orchestrate_research_with_i04(
        previous=previous, prior_i06=prior_i06,
        expiry_minutes=expiry_minutes, now=now, provider=knowledge_provider,
    )
    return await asyncio.to_thread(
        enrich_research_with_i05, source, now=now,
        registry=registry_provider, knowledge=knowledge_provider,
    )

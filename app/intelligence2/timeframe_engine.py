"""I03 Timeframe Engine: public research-only hierarchical context alignment.

One integration of the I02 scanner's public ranked assets with I01 public
closed-candle windows and the shared Context Engine; no independent scanner,
scheduler, broker client, datastore, user training data or execution path.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time

from ..market import MarketError
from . import market_data
from .context_engine import VERSION as CONTEXT_VERSION, analyze_context

VERSION = "intelligence2_timeframes_v1"
EXPIRIES = frozenset((1, 3, 5, 15))
MAX_CONTEXT_CANDIDATES = 8
MAX_CONTEXT_CONCURRENCY = 2
ROLE_NAMES = ("higher", "intermediate", "lower")


def choose_hierarchy(asset: dict, *, expiry_minutes: int) -> dict:
    """Transparent research profile; never invent an unsupported candle TF.

    Profiles only use I01-supported timeframes. An expiry is a *hypothesis*
    parameter, not a broker-verified contract duration.
    """
    if type(expiry_minutes) is not int or expiry_minutes not in EXPIRIES:
        raise ValueError("I03: pesquisa com expiração não suportada")
    if not isinstance(asset, dict) or asset.get("eligible") is not True:
        raise ValueError("I03: ativo sem seleção pública elegível")
    if asset.get("execution_allowed") is not False:
        raise ValueError("I03: autorização financeira nunca é aceita")
    if asset.get("candle_quality") != "COMPLETE":
        raise ValueError("I03: qualidade da I02 não comprovada")
    vol = asset.get("realized_volatility")
    if (isinstance(vol, bool) or not isinstance(vol, (int, float))
        or not math.isfinite(vol) or vol < 0):
        raise ValueError("I03: volatilidade pública não verificável")
    market = asset.get("market")
    family = asset.get("strategy_family")
    regime = asset.get("market_regime")
    if not all(isinstance(x, str) and x for x in (market, family, regime)):
        raise ValueError("I03: contexto inicial ausente")

    stress = vol >= .012 or regime in ("HIGH_VOLATILITY", "IRREGULAR")
    fast_family = market == "derived" and family in (
        "step", "volatility", "range_break"
    )
    if expiry_minutes <= 3:
        profile = (3600, 300, 60) if stress else (900, 300, 60)
        reason = "EXPIRY_SHORT_STRESS" if stress else "EXPIRY_SHORT"
    elif expiry_minutes == 5 and fast_family and not stress:
        profile = (900, 300, 60)
        reason = "DERIVED_FAST_FAMILY"
    else:
        profile = (3600, 900, 300)
        reason = "LONGER_EXPIRY_OR_STRESS"
    return {
        "version": VERSION, "higher_seconds": profile[0],
        "intermediate_seconds": profile[1], "lower_seconds": profile[2],
        "expiry_minutes": expiry_minutes,
        "selector_inputs": {
            "market": market, "strategy_family": family,
            "i02_regime": regime, "i02_realized_volatility": vol,
            "i02_quality": asset.get("candle_quality"),
        },
        "reason_codes": [reason, "I01_SUPPORTED_CLOSED_TIMEFRAMES"],
        "broker_contract_expiry_verified": False,
    }


def _decision(contexts: dict, *, profile: dict) -> dict:
    """Conservative conflict resolution using independent closed TF evidence."""
    high, mid, low = (contexts.get(k, {}) for k in ROLE_NAMES)
    if any(
        not isinstance(ctx, dict) or ctx.get("state") in (
            "INDETERMINATE", "IRREGULAR"
        ) or ctx.get("research_usable") is not True
        for ctx in (high, mid, low)
    ):
        return {
            "state": "INDETERMINATE", "direction": "NEUTRAL",
            "reason_codes": ["INVALID_OR_IRREGULAR_TIMEFRAME"],
            "requires_recheck": True, "strategy_research_usable": False,
        }

    hd, md, ld = (c.get("direction", "NEUTRAL") for c in (high, mid, low))
    if any(d not in ("UP", "DOWN", "NEUTRAL") for d in (hd, md, ld)):
        return {
            "state": "INDETERMINATE", "direction": "NEUTRAL",
            "reason_codes": ["UNKNOWN_DIRECTION"], "requires_recheck": True,
            "strategy_research_usable": False,
        }
    # Strong disagreement never promotes an exploratory opportunity.
    high_strong = high.get("strength", 0) >= .60 and high.get("stability", 0) >= .65
    mid_strong = mid.get("strength", 0) >= .60 and mid.get("stability", 0) >= .65
    lower_strong = low.get("strength", 0) >= .60 and low.get("stability", 0) >= .65

    if hd in ("UP", "DOWN") and md in ("UP", "DOWN") and hd != md:
        if high_strong and mid_strong:
            return {
                "state": "BLOCKED_STRONG_CONFLICT", "direction": "NEUTRAL",
                "reason_codes": ["HIGHER_INTERMEDIATE_STRONGLY_OPPOSED"],
                "requires_recheck": True, "strategy_research_usable": False,
            }
        # An *observational* reversal requires weak higher TF evidence,
        # two confirming smaller TFs, strong/stable evidence, and two closed
        # directional candles on BOTH lower timeframes.
        confirmed = all((
            not high_strong, high.get("strength", 1) < .45,
            md == ld, mid_strong, lower_strong,
            mid.get("confidence", 0) >= .68,
            low.get("confidence", 0) >= .68,
            mid.get("evidence", {}).get("two_closed_candles_confirm_direction") is True,
            low.get("evidence", {}).get("two_closed_candles_confirm_direction") is True,
        ))
        if confirmed:
            return {
                "state": "REVERSAL_CONFIRMED_RESEARCH", "direction": md,
                "reason_codes": [
                    "WEAK_HIGHER_REGIME",
                    "INTERMEDIATE_AND_LOWER_AGREE",
                    "TWO_CLOSED_CONFIRMATIONS_EACH",
                    "RESEARCH_DIRECTION_ONLY",
                ],
                "requires_recheck": True, "strategy_research_usable": True,
            }
        return {
            "state": "WAIT_REEVALUATE", "direction": "NEUTRAL",
            "reason_codes": ["MODERATE_HIGHER_INTERMEDIATE_CONFLICT"],
            "requires_recheck": True, "strategy_research_usable": False,
        }

    if hd in ("UP", "DOWN") and md == hd and ld == hd:
        # Even full direction agreement must not overrule weak TF evidence.
        if min(
            high.get("stability", 0), mid.get("stability", 0),
            low.get("stability", 0),
        ) < .45:
            return {
                "state": "WAIT_REEVALUATE", "direction": "NEUTRAL",
                "reason_codes": ["DIRECTION_ALIGNED_BUT_UNSTABLE"],
                "requires_recheck": True, "strategy_research_usable": False,
            }
        return {
            "state": "ALIGNED_RESEARCH", "direction": hd,
            "reason_codes": ["THREE_CLOSED_TIMEFRAMES_ALIGNED"],
            "requires_recheck": False, "strategy_research_usable": True,
        }
    if hd in ("UP", "DOWN") and md == hd and ld != hd:
        if ld in ("UP", "DOWN") and high_strong and lower_strong:
            return {
                "state": "BLOCKED_STRONG_CONFLICT", "direction": "NEUTRAL",
                "reason_codes": ["STRONG_LOWER_REVERSAL_NOT_CONFIRMED_ON_MIDDLE"],
                "requires_recheck": True, "strategy_research_usable": False,
            }
        return {
            "state": "WAIT_REEVALUATE", "direction": "NEUTRAL",
            "reason_codes": ["LOWER_TIMEFRAME_NOT_CONFIRMED"],
            "requires_recheck": True, "strategy_research_usable": False,
        }
    return {
        "state": "WAIT_REEVALUATE", "direction": "NEUTRAL",
        "reason_codes": ["NEUTRAL_OR_MIXED_TIMEFRAMES"],
        "requires_recheck": True, "strategy_research_usable": False,
    }


def align_timeframes(contexts: dict, *, profile: dict) -> dict:
    """Immutable structured handoff for future research engines, not orders."""
    verdict = _decision(contexts, profile=profile)
    confidence = min(
        (c.get("confidence", 0) for c in contexts.values()),
        default=0.0,
    ) if verdict["strategy_research_usable"] else 0.0
    return {
        "version": VERSION,
        "profile": profile,
        "contexts": {k: contexts.get(k) for k in ROLE_NAMES},
        "alignment": verdict,
        "overall_confidence": round(confidence, 5),
        "confidence_kind": "HEURISTIC_NOT_PROBABILISTIC",
        "approved_for_trading": False,
        "execution_allowed": False,
        "model_validation": {"status": "NO_VALIDATED_MODEL_AVAILABLE", "used": False},
    }


async def _analyze_one(asset: dict, *, observed_at: int,
                       expiry_minutes: int, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        try:
            hierarchy = choose_hierarchy(asset, expiry_minutes=expiry_minutes)
        except (ValueError, TypeError):
            return {
                "version": VERSION, "symbol": asset.get("symbol"),
                "state": "INDETERMINATE",
                "reason_codes": ["INVALID_I02_SELECTION_INPUT"],
                "execution_allowed": False, "approved_for_trading": False,
            }
        contexts = {}
        for role, seconds in zip(ROLE_NAMES, (
            hierarchy["higher_seconds"], hierarchy["intermediate_seconds"],
            hierarchy["lower_seconds"],
        )):
            try:
                window = await market_data.read_closed_window(
                    asset["symbol"], seconds, 96, now=observed_at,
                    timeout=8.0,
                )
            except (MarketError, TimeoutError, OSError, ValueError, TypeError):
                window = {"quality": "UNAVAILABLE", "usable_for_research": False}
            contexts[role] = analyze_context(
                window, market=asset["market"], symbol=asset["symbol"],
                timeframe_seconds=seconds, observed_at=observed_at,
            )
        result = align_timeframes(contexts, profile=hierarchy)
        # Opaque provenance of PUBLIC metadata only (not a user/account key).
        fingerprint = hashlib.sha256(json.dumps({
            "symbol": asset["symbol"], "market": asset["market"],
            "profile": hierarchy, "context_version": CONTEXT_VERSION,
            "context_anchors": [
                contexts[k].get("last_closed_epoch") for k in ROLE_NAMES
            ],
            "context_states": [
                contexts[k]["state"] for k in ROLE_NAMES
            ],
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {
            "symbol": asset["symbol"], "market": asset["market"],
            "observed_at_epoch": observed_at, "version": VERSION,
            "provenance_sha256": fingerprint,
            "state": result["alignment"]["state"],
            **result,
        }


def _interleave_selection(markets: dict) -> list[tuple[str, dict]]:
    """Fair round-robin of I02 primary assets across all broker markets."""
    markets_ordered = sorted(markets)
    max_count = max(
        (len(markets[m].get("selected", [])) for m in markets_ordered), default=0
    )
    result = []
    for rank in range(max_count):
        for market in markets_ordered:
            items = markets[market].get("selected", [])
            if rank < len(items):
                result.append((market, items[rank]))
    return result


async def enrich_i02_report(i02_report: dict, *, expiry_minutes: int = 5,
                            now: int | None = None) -> dict:
    """Versioned opt-in enrichment of I02's public report, never user data.

    The existing scanner endpoint owns the only final report cache; I03
    creates no parallel feed/cache. Unevaluated candidates explicitly say so.
    """
    if type(expiry_minutes) is not int or expiry_minutes not in EXPIRIES:
        raise MarketError("I03: expiração de pesquisa inválida")
    if not isinstance(i02_report, dict) or not isinstance(i02_report.get("markets"), dict):
        raise MarketError("I03: relatório público I02 inválido")
    if now is None:
        now = int(time.time())
    if type(now) is not int or now <= 0:
        raise MarketError("I03: relógio inválido")
    markets = i02_report["markets"]
    queue = _interleave_selection(markets)
    chosen = queue[:MAX_CONTEXT_CANDIDATES]
    deferred = queue[MAX_CONTEXT_CANDIDATES:]
    sem = asyncio.Semaphore(MAX_CONTEXT_CONCURRENCY)
    results = await asyncio.gather(*(
        _analyze_one(asset, observed_at=now, expiry_minutes=expiry_minutes,
                     semaphore=sem)
        for _, asset in chosen
    ))
    analyses = {(market, asset["symbol"]): answer
                for (market, asset), answer in zip(chosen, results)}
    new_markets = {}
    states = []
    for market, section in sorted(markets.items()):
        primary = []
        for asset in section.get("selected", []):
            explanation = analyses.get((market, asset["symbol"]))
            if explanation is None:
                explanation = {
                    "version": VERSION, "symbol": asset["symbol"],
                    "state": "NOT_EVALUATED_CAPACITY",
                    "reason_codes": ["BOUNDED_CONTEXT_COVERAGE"],
                    "approved_for_trading": False, "execution_allowed": False,
                }
            states.append(explanation["state"])
            primary.append({**asset, "context_timeframe": explanation,
                            "execution_allowed": False})
        new_markets[market] = {**section, "selected": primary}
    return {
        **i02_report,
        "version": "intelligence2_i03_v1",
        "markets": new_markets,
        "i02_source_version": i02_report.get("version"),
        "context_engine_version": CONTEXT_VERSION,
        "timeframe_engine_version": VERSION,
        "expiry_minutes_research": expiry_minutes,
        "observed_at_epoch": now,
        "context_evaluated": len(chosen),
        "context_deferred_capacity": len(deferred),
        "context_coverage_complete": not deferred,
        "context_states": {
            status: states.count(status) for status in sorted(set(states))
        },
        "shared_public_knowledge": True,
        "confidence_kind": "HEURISTIC_NOT_PROBABILISTIC",
        "orders_sent": 0,
        "execution_allowed": False,
        "approved_for_trading": False,
    }

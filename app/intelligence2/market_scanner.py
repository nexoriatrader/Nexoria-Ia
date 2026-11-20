"""I02: dynamic, explainable, read-only selection over the EXISTING scanner.

This module does not own a feed, cache, task scheduler, signal dispatcher or
ledger. It reuses automation_markets.market_instruments (broker catalogue and
existing cache), I01 read_closed_window (public candle dedup + strict OHLC),
market.discover_contracts and the legacy asset_scanner/autopilot classifiers.
A research score is NEVER a prediction, purchase signal or broker permission.
"""
from __future__ import annotations

import asyncio
import math
import re
import statistics
import time
from collections import defaultdict

from sqlalchemy import or_, select

from .. import automation_markets, db
from ..asset_scanner import scanner_family
from ..autopilot import analyze
from ..market import MarketError, discover_contracts
from .market_data import read_closed_window

PRIMARY_PER_MARKET = 3
RESERVES_PER_MARKET = 2
# Bounded sampling avoids exhausting the pre-existing public WebSocket pool.
# The offset rotates every 5 minutes, covering every broker-listed symbol
# over multiple runs. Coverage is reported and never represented as exhaustive.
SAMPLE_PER_MARKET = 8
MAX_PARALLEL_READS = 3
M1_WINDOW = 96
MIN_CLOSED_BARS = 72
_ACTIVE = frozenset(("PENDING", "UNKNOWN", "CONFIRMED"))
_SYMBOL = re.compile(r"[A-Za-z0-9_]{2,30}\Z")


def _pick_window(rows: list[dict], observed_at: int, *,
                 focus: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Recheck last primary/reserve assets, rotate the remaining catalogue.

    A reserve is monitored lightly on each requested scan (no worker/loop),
    while spare bounded capacity rotates through other listed assets.
    """
    ordered = sorted(rows, key=lambda x: x["symbol"])
    focus = focus or set()
    incumbents = [row for row in ordered if row["symbol"] in focus][:SAMPLE_PER_MARKET]
    remaining = [row for row in ordered if row["symbol"] not in {
        item["symbol"] for item in incumbents
    }]
    slots = SAMPLE_PER_MARKET - len(incumbents)
    if slots == 0:
        return incumbents, remaining
    if len(remaining) <= slots:
        return incumbents + remaining, []
    pages = math.ceil(len(remaining) / slots)
    start = ((observed_at // 300) % pages) * slots
    sampled = remaining[start:start + slots]
    return incumbents + sampled, remaining[:start] + remaining[start + slots:]


def _excluded(asset: dict, code: str, explanation: str, *, detail=None) -> dict:
    return {
        "symbol": asset["symbol"],
        "market": asset["group"],
        "name": asset["name"],
        "eligible": False,
        "score": None,
        "status": "EXCLUDED",
        "reason_codes": [code],
        "reason": explanation,
        "detail": detail,
        "execution_allowed": False,
    }


def _research_metrics(bars: list[dict], family: str, public_open: bool | None) -> dict:
    """Describe present market conditions; no future outcomes or P&L."""
    closes = [bar["close"] for bar in bars]
    returns = [(b - a) / a for a, b in zip(closes[-61:-1], closes[-60:])]
    volatility = statistics.pstdev(returns)
    recent = closes[-31:]
    path = sum(abs(b - a) for a, b in zip(recent, recent[1:]))
    efficiency = abs(recent[-1] - recent[0]) / path if path else 0.0
    regime = (
        "HIGH_VOLATILITY" if volatility > 0.025 else
        "TRENDING" if efficiency >= 0.55 else
        "RANGING" if efficiency <= 0.20 else "MIXED"
    )
    # Explainable, bounded heuristics (NOT expected returns). Data quality and
    # freshness are prerequisites and must never be compensated by a score.
    score_parts = {
        "closed_candle_quality": 25.0,
        "freshness": 15.0,
        "relative_volatility": round(max(
            0.0, 20.0 - abs(math.log10(volatility / 0.001)) * 6.0
        ), 2) if volatility > 1e-8 else 0.0,
        "trend_structure": round(5.0 + 10.0 * efficiency, 2),
        "existing_strategy_family": 14.0 if family != "generic" else 9.0,
        "public_contract_conditions": 10.0 if public_open is True else 6.0,
    }
    return {
        "regime": regime,
        "volatility": round(volatility, 8),
        "trend_efficiency": round(efficiency, 5),
        "score_components": score_parts,
        "score": round(sum(score_parts.values()), 2),
    }


async def _inspect(asset: dict, *, observed_at: int, semaphore: asyncio.Semaphore) -> dict:
    """One broker catalogue asset; no account/token/financial interactions."""
    async with semaphore:
        if asset["group"] == "unclassified":
            return _excluded(asset, "MARKET_UNCLASSIFIED", "Mercado sem metadados verificáveis")
        if asset.get("catalogue_open") is False:
            return _excluded(asset, "SESSION_CLOSED", "Catálogo público informa mercado fechado")
        if asset.get("catalogue_suspended") is True:
            return _excluded(asset, "TRADING_SUSPENDED", "Catálogo público informa ativo suspenso")
        try:
            window = await read_closed_window(
                asset["symbol"], 60, M1_WINDOW, now=observed_at, timeout=8.0,
            )
            if not window.get("usable_for_research") or window.get("quality") != "COMPLETE":
                return _excluded(
                    asset, "CANDLE_" + str(window.get("quality", "INVALID")),
                    "Dados incompletos, atrasados ou inconsistentes",
                    detail={"quality": window.get("quality"),
                            "missing_intervals": window.get("missing_intervals")},
                )
            if window.get("closed_count", 0) < MIN_CLOSED_BARS:
                return _excluded(
                    asset, "INSUFFICIENT_HISTORY",
                    "Amostra de candles fechados insuficiente para classificação",
                    detail={"closed_count": window.get("closed_count")},
                )
            bars = window["candles"]
            # I01 guarantees chronological OHLC and no duplicate candle. The
            # scanner independently requires usable contiguity from I01.
            if not bars or len(bars) < MIN_CLOSED_BARS:
                return _excluded(asset, "INSUFFICIENT_HISTORY", "Histórico incompleto")
            family = scanner_family(asset["name"]) or "generic"
            metrics = _research_metrics(bars, family, asset.get("catalogue_open"))
            vol = metrics["volatility"]
            if vol <= 1e-8:
                return _excluded(asset, "FLAT_MARKET", "Variação observada insuficiente")
            if vol > 0.05:
                return _excluded(asset, "EXCESSIVE_VOLATILITY", "Variação recente excessiva para seleção conservadora")
            contract_types = await discover_contracts(asset["symbol"])
            if not isinstance(contract_types, (list, tuple, set)) or not {"CALL", "PUT"}.issubset(set(contract_types)):
                return _excluded(
                    asset, "CALL_PUT_UNAVAILABLE",
                    "Catálogo público não confirmou ambos os tipos CALL/PUT",
                )
            research = analyze([bar["close"] for bar in bars])
            return {
                "symbol": asset["symbol"], "market": asset["group"],
                "name": asset["name"], "eligible": True,
                "status": "RESEARCH_ELIGIBLE",
                "score": metrics["score"], "score_components": metrics["score_components"],
                "reason_codes": [
                    "CLOSED_CANDLES_COMPLETE", "LATEST_CANDLE_CURRENT",
                    "PUBLIC_CALL_PUT_LISTED", "RESEARCH_STRATEGY_COMPATIBLE",
                    "SESSION_OPEN" if asset.get("catalogue_open") is True
                    else "SESSION_NOT_VERIFIED",
                ],
                "reason": "Elegível para comparação de pesquisa; nenhuma compra autorizada",
                "market_regime": metrics["regime"],
                "realized_volatility": metrics["volatility"],
                "trend_efficiency": metrics["trend_efficiency"],
                "strategy_family": family,
                "research_state": research["state"],
                "research_hypotheses_count": len(research["candidates"]),
                "candle_quality": window["quality"],
                "closed_candles": window["closed_count"],
                "last_closed_epoch": window["last_closed_epoch"],
                "execution_conditions": {
                    "public_session": (
                        "OPEN" if asset.get("catalogue_open") is True else "UNVERIFIED"
                    ),
                    "call_put_listed": True,
                    "contract_duration_verified": False,
                    "broker_payout_verified": False,
                    "account_eligibility_verified": False,
                    "network_execution_verified": False,
                },
                # Read-only structured handoff for I03; not a dispatch message.
                "context_inputs": {
                    "symbol": asset["symbol"], "market": asset["group"],
                    "market_regime": metrics["regime"],
                    "data_quality": window["quality"],
                    "observed_at_epoch": observed_at,
                },
                "timeframe_inputs": {
                    "granularity_seconds": 60,
                    "last_closed_epoch": window["last_closed_epoch"],
                    "closed_candles": window["closed_count"],
                },
                "strategy_inputs": {
                    "family": family, "research_state": research["state"],
                },
                "execution_allowed": False,
            }
        except (MarketError, ValueError, TypeError, ArithmeticError, KeyError, IndexError) as exc:
            return _excluded(
                asset, "PUBLIC_RESEARCH_UNAVAILABLE",
                "Validação pública indisponível ou dados incompatíveis",
                detail={"exception_type": type(exc).__name__},
            )


def _previous_symbols(previous: dict | None, market: str, role: str) -> set[str]:
    if not isinstance(previous, dict):
        return set()
    groups = previous.get("markets")
    if not isinstance(groups, dict):
        return set()
    section = groups.get(market)
    if not isinstance(section, dict):
        return set()
    rows = section.get(role, [])
    if not isinstance(rows, list):
        return set()
    return {
        row["symbol"] for row in rows if isinstance(row, dict)
        and isinstance(row.get("symbol"), str)
        and _SYMBOL.fullmatch(row["symbol"])
    }


async def scan_market_opportunities(*, previous: dict | None = None,
                                    now: float | None = None) -> dict:
    """Scan EVERY listed market, with honest per-market rotating coverage.

    All results are public and user-agnostic, suitable for the *existing*
    authenticated scanner endpoint's shared result cache. Per-user open
    contracts are attached separately after cache retrieval.
    """
    if (now is not None and (isinstance(now, bool)
        or not isinstance(now, (int, float)) or not math.isfinite(now) or now <= 0)):
        raise MarketError("Horário de scanner inválido")
    observed_at = int(time.time() if now is None else now)
    catalogue = await automation_markets.market_instruments()
    if not isinstance(catalogue, list):
        raise MarketError("Catálogo público não verificável")
    grouped = defaultdict(list)
    for asset in catalogue:
        if (isinstance(asset, dict) and isinstance(asset.get("symbol"), str)
            and _SYMBOL.fullmatch(asset["symbol"])
            and isinstance(asset.get("group"), str)
            and isinstance(asset.get("name"), str)):
            grouped[asset["group"]].append(asset)
    samples, deferred = {}, {}
    for market_name, items in sorted(grouped.items()):
        samples[market_name], deferred[market_name] = _pick_window(
            items, observed_at,
            focus=(_previous_symbols(previous, market_name, "selected")
                   | _previous_symbols(previous, market_name, "reserves")),
        )
    sem = asyncio.Semaphore(MAX_PARALLEL_READS)
    jobs = [
        (market_name, asset) for market_name, rows in samples.items()
        for asset in rows
    ]
    assessments = await asyncio.gather(*(
        _inspect(asset, observed_at=observed_at, semaphore=sem)
        for _, asset in jobs
    ))
    by_market = defaultdict(list)
    for (market_name, _), assessment in zip(jobs, assessments):
        by_market[market_name].append(assessment)

    markets = {}
    selected_total = 0
    for market_name, items in sorted(grouped.items()):
        assessed = by_market[market_name]
        eligible = sorted(
            (a for a in assessed if a["eligible"]),
            key=lambda a: (-a["score"], a["symbol"]),
        )
        selected = [dict(item, rank=i + 1, role="PRIMARY")
                    for i, item in enumerate(eligible[:PRIMARY_PER_MARKET])]
        reserves = [
            dict(item, rank=i + PRIMARY_PER_MARKET + 1, role="LIGHT_RESERVE",
                 monitoring="RECHECK_ON_NEXT_SCAN")
            for i, item in enumerate(eligible[PRIMARY_PER_MARKET:
                                             PRIMARY_PER_MARKET + RESERVES_PER_MARKET])
        ]
        for item in deferred[market_name]:
            assessed.append(_excluded(
                item, "DEFERRED_CAPACITY",
                "Aguardando próximo lote rotativo; não avaliado neste ciclo",
            ))
        previous_set = _previous_symbols(previous, market_name, "selected")
        selected_set = {item["symbol"] for item in selected}
        movements = {
            "added": sorted(selected_set - previous_set),
            "removed": sorted(previous_set - selected_set),
            "not_rechecked": sorted(
                previous_set & {a["symbol"] for a in deferred[market_name]}
            ),
            "scope": "IN_PROCESS_REPORT_ONLY",
        }
        selected_total += len(selected)
        markets[market_name] = {
            "catalogue_assets": len(items),
            "inspected_assets": len(samples[market_name]),
            "deferred_assets": len(deferred[market_name]),
            "coverage_complete": not deferred[market_name],
            "selected": selected,
            "reserves": reserves,
            "assessments": sorted(assessed, key=lambda a: a["symbol"]),
            "selection_changes": movements,
            "state": "RESEARCH_ONLY" if selected else "NO_OPPORTUNITIES",
        }
    return {
        "version": "intelligence2_i02_v1",
        "state": "RESEARCH_ONLY" if selected_total else "NO_OPPORTUNITIES",
        "reason": (
            "Ranking técnico de pesquisa; condições financeiras NÃO verificadas"
            if selected_total else
            "Nenhum ativo elegível na amostra examinada; ativos adiados não foram avaliados"
        ),
        "observed_at_epoch": observed_at,
        "markets": markets,
        "catalogue_markets": len(grouped),
        "catalogue_assets": sum(len(v) for v in grouped.values()),
        "inspected_assets": len(jobs),
        "selected_total": selected_total,
        "scan_coverage_complete": not any(deferred.values()),
        "selection_mode": "DYNAMIC_PUBLIC_RESEARCH_ONLY",
        "selection_scope": "EVALUATED_SAMPLE_NOT_GLOBAL_MARKET_RANKING",
        "cache_owner": "existing_authenticated_asset_scanner_route",
        "orders_sent": 0,
        "execution_allowed": False,
    }


def open_contracts_for_user(session, user_id: int) -> list[dict]:
    """Read-only continuity overlay. Never select/cancel/settle any order.

    Both modalities are scoped to the authenticated user. These records are
    NOT part of the shared scanner cache, preventing cross-user disclosure.
    A terminal row with an anomalous active slot is included for review.
    """
    result = []
    for model, mode in ((db.DemoOrder, "demo"), (db.RealOrder, "real")):
        rows = session.scalars(select(model).where(
            model.user_id == user_id,
            or_(model.state.in_(_ACTIVE), model.active_slot.is_not(None)),
        ).order_by(model.id.asc())).all()
        for row in rows:
            result.append({
                "order_id": row.id,
                "account_mode": mode,
                "account_id": row.account_id if mode == "real" else None,
                "symbol": row.symbol,
                "state": row.state,
                "slot_held": row.active_slot is not None,
                "preservation": "INDEPENDENT_OF_MARKET_SELECTION",
                "requires_reconciliation": row.state in _ACTIVE
                                          or row.active_slot is not None,
            })
    return result

"""NEXORA 2.0: reproducible, non-trading analysis of user-provided historical M1 OHLC.

This pipeline orchestrates the existing Context and Timeframe engines.
Never treats user files as authenticated broker data. No forecasts, contracts,
order proposals, model training or financial execution.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import time

from .context_engine import VERSION as CONTEXT_VERSION, analyze_context
from .timeframe_engine import VERSION as TF_VERSION, align_timeframes

VERSION = "nexora2_uploaded_ohlc_v1"
SOURCE = "USER_SUPPLIED_UNVERIFIED_OHLC"
MIN_BARS = 72
MAX_BARS = 1500
SYMBOL_PATTERN = re.compile(r"[A-Za-z0-9_]{2,30}\Z")
TF_NAMES = {"higher": 900, "intermediate": 300, "lower": 60}


class ResearchInputError(ValueError):
    """Input rejected before analysis or any database mutation."""


def normalize_input(payload: dict, *, clock: int | None = None) -> dict:
    if not isinstance(payload, dict):
        raise ResearchInputError("Envie um objeto JSON com símbolo e candles")
    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
        raise ResearchInputError("Símbolo inválido (2 a 30 letras, números ou _)")
    if payload.get("granularity_seconds") != 60 or type(payload.get("granularity_seconds")) is not int:
        raise ResearchInputError("A pesquisa importada exige candles M1 (60 segundos)")
    raw = payload.get("candles")
    if not isinstance(raw, list) or not MIN_BARS <= len(raw) <= MAX_BARS:
        raise ResearchInputError("Informe de 72 a 1500 candles M1")
    current = int(time.time() if clock is None else clock)
    if type(current) is not int or current <= 0:
        raise ResearchInputError("Relógio inválido")
    normalized = []
    previous = None
    for index, candle in enumerate(raw):
        if not isinstance(candle, dict) or type(candle.get("epoch")) is not int:
            raise ResearchInputError(f"Candle {index+1}: timestamp inválido")
        epoch = candle["epoch"]
        if epoch <= 0 or epoch % 60 != 0:
            raise ResearchInputError(f"Candle {index+1}: epoch fora do minuto")
        if previous is not None and epoch - previous != 60:
            raise ResearchInputError(f"Candle {index+1}: ordem, repetição ou lacuna no OHLC")
        previous = epoch
        values = {}
        for name in ("open", "high", "low", "close"):
            value = candle.get(name)
            if type(value) not in (int, float):
                raise ResearchInputError(f"Candle {index+1}: {name} não é numérico")
            number = float(value)
            if not math.isfinite(number) or not 0 < number <= 1_000_000_000:
                raise ResearchInputError(f"Candle {index+1}: {name} fora do intervalo válido")
            values[name] = number
        if (values["low"] > min(values["open"], values["close"])
            or values["high"] < max(values["open"], values["close"])
            or values["low"] > values["high"]):
            raise ResearchInputError(f"Candle {index+1}: máxima/mínima incoerente")
        normalized.append({"epoch": epoch, **values})
    if normalized[-1]["epoch"] + 60 > current:
        raise ResearchInputError("O último candle ainda não está encerrado")
    return {"symbol": symbol, "granularity_seconds": 60, "candles": normalized}


def aggregate_closed_minutes(candles: list[dict], seconds: int) -> list[dict]:
    """Form only complete aligned M5/M15 periods. Never invent missing bars."""
    if seconds == 60:
        return candles
    expected = seconds // 60
    aggregates = []
    current_bucket = None
    group = []

    def emit(bucket, data):
        if (bucket is not None and len(data) == expected
            and data[0]["epoch"] == bucket
            and data[-1]["epoch"] == bucket + seconds - 60):
            aggregates.append({
                "epoch": bucket,
                "open": data[0]["open"],
                "high": max(item["high"] for item in data),
                "low": min(item["low"] for item in data),
                "close": data[-1]["close"],
            })

    for candle in candles:
        bucket = candle["epoch"] // seconds * seconds
        if current_bucket is not None and bucket != current_bucket:
            emit(current_bucket, group)
            group = []
        current_bucket = bucket
        group.append(candle)
    emit(current_bucket, group)
    return aggregates


def describe_returns(candles: list[dict]) -> dict:
    closes = [row["close"] for row in candles]
    change = [(b - a) / a for a, b in zip(closes, closes[1:])]
    ordered = sorted(change)
    count = len(change)
    up = sum(a > 0 for a in change)
    down = sum(a < 0 for a in change)
    flat = count - up - down
    spread = [row["high"] - row["low"] for row in candles]
    middle = statistics.median(spread)
    anomalies = sum((value > middle * 8 and middle > 0)
                    for value in spread)
    return {
        "periods": len(candles), "changes_observed": count,
        "up_intervals": up, "down_intervals": down, "unchanged_intervals": flat,
        "mean_change": round(statistics.mean(change), 9),
        "median_change": round(statistics.median(change), 9),
        "realized_volatility": round(statistics.pstdev(change), 9),
        "historical_p10": round(ordered[count // 10], 9),
        "historical_p90": round(ordered[min(count-1, count * 9 // 10)], 9),
        "unusually_wide_candles": anomalies,
        "interpretation": "DESCRIPTIVE_NOT_PREDICTIVE",
    }


def build_report(payload: dict, *, clock: int | None = None) -> dict:
    normalized = normalize_input(payload, clock=clock)
    bars = normalized["candles"]
    last_closed = bars[-1]["epoch"]
    observed_at = last_closed + 60
    symbol = normalized["symbol"]
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"),
                           allow_nan=False).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    contexts = {}
    diagnostics = []
    for role, seconds in TF_NAMES.items():
        sampled = aggregate_closed_minutes(bars, seconds)
        eligible = len(sampled) >= MIN_BARS
        # M5/M15 can end earlier than the last M1 when the final bucket is
        # incomplete. Analyze each at its own *last complete close*, never
        # claim that a partial M15 bar is valid or fabricate future minutes.
        role_asof = sampled[-1]["epoch"] + seconds if sampled else None
        if role_asof is not None and role_asof > observed_at:
            raise ResearchInputError("Timeframe agregado ultrapassa o último candle encerrado")
        window = {
            "quality": "COMPLETE" if eligible else "INSUFFICIENT",
            "usable_for_research": eligible,
            "candles": sampled,
            "closed_count": len(sampled),
            "last_closed_epoch": sampled[-1]["epoch"] if sampled else None,
        }
        context = analyze_context(window, market="user_supplied",
                                  symbol=symbol, timeframe_seconds=seconds,
                                  observed_at=role_asof if role_asof is not None else observed_at)
        contexts[role] = context
        diagnostics.append({
            "module": "CONTEXT_" + role.upper(),
            "version": CONTEXT_VERSION,
            "state": context["state"],
            "bars": len(sampled),
            "quality": window["quality"],
            "asof_epoch": role_asof,
            "replay_cutoff_epoch": observed_at,
            "reason_codes": context["reason_codes"],
        })
    profile = {
        "version": VERSION, "higher_seconds": 900, "intermediate_seconds": 300,
        "lower_seconds": 60, "source": SOURCE,
        "replay_asof_epoch": observed_at,
    }
    alignment = align_timeframes(contexts, profile=profile)
    stats = describe_returns(bars)
    return {
        "version": VERSION, "source": SOURCE, "symbol": symbol,
        "created_from": "USER_UPLOAD", "input": normalized,
        "period_from_epoch": bars[0]["epoch"],
        "period_to_epoch": observed_at,
        "evidence": {
            "input_sha256": digest,
            "input_count": len(bars),
            "input_granularity_seconds": 60,
            "all_input_bars_closed_at_replay_cutoff": True,
            "timeframe_asof_epochs": {
                row["module"]: row["asof_epoch"] for row in diagnostics
            },
            "gaps_filled": 0,
            "market_source_verified": False,
        },
        "orchestrator": {"version": VERSION, "stages": [
            {"module": "OHLC_VALIDATION", "state": "COMPLETE",
             "input_count": len(bars)},
            {"module": "STATISTICAL_INTELLIGENCE", "state": "COMPLETE",
             "version": VERSION},
            *diagnostics,
            {"module": "TIMEFRAME_ALIGNMENT", "version": TF_VERSION,
             "state": alignment["alignment"]["state"]},
            {"module": "EVIDENCE_DIGEST", "state": "COMPLETE"},
        ]},
        "statistics": stats, "contexts": contexts,
        "timeframe_alignment": alignment,
        "models": {"status": "NO_VALIDATED_MODEL_AVAILABLE",
                   "champion": None, "challenger": None, "fallback": None},
        "orders_sent": 0, "execution_allowed": False,
        "disclaimer": "Pesquisa descritiva de dados enviados pelo usuário; "
                      "procedência não verificada, sem previsão ou resultado financeiro.",
    }


def summary(report: dict) -> dict:
    alignment = report["timeframe_alignment"]
    return {
        "version": report["version"], "source": report["source"],
        "symbol": report["symbol"], "period_from_epoch": report["period_from_epoch"],
        "period_to_epoch": report["period_to_epoch"],
        "evidence": report["evidence"],
        "statistics": report["statistics"],
        "contexts": {role: {
            "state": c["state"], "data_quality": c["data_quality"],
            "reason_codes": c["reason_codes"],
            "confidence_kind": c["confidence_kind"],
        } for role, c in report["contexts"].items()},
        "timeframe_alignment": {
            "state": alignment["alignment"]["state"],
            "reason_codes": alignment["alignment"]["reason_codes"],
            "confidence_kind": alignment["confidence_kind"],
        },
        "orchestrator": report["orchestrator"],
        "models": report["models"],
        "orders_sent": 0, "execution_allowed": False,
        "disclaimer": report["disclaimer"],
    }

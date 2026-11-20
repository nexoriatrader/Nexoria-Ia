"""I05 Learning Engine: public market-regime research, NEVER trading.

One knowledge chain: I04 verified MARKET knowledge -> I01 closed OHLC ->
I03 shared Context Engine -> chronological regime-transition research model ->
I04 MODEL metadata/evidence -> append-only research release ledger.

This predicts classification of *future regime labels*, not price, payoff,
signal direction or trading success. It consumes NO user/private financial data,
does not buy, cancel, settle, change strategies or call any execution module.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, or_
from sqlalchemy.exc import IntegrityError

from .. import db
from ..research_persistence import HistoricalResearchReport
from . import market_data
from .context_engine import (
    VERSION as CONTEXT_VERSION, MIN_BARS, analyze_context,
)
from . import knowledge_base as kb

VERSION = "intelligence2_learning_regimes_v1"
SOURCE_VERSION = "i05_public_closed_regimes_v1"
ARTIFACT_KEY = "I05_PUBLIC_REGIME_ARTIFACT_V1"
RELEASE_KEY = "I05_PUBLIC_REGIME_RELEASE_V1"
EVENT_KEY = "I05_PUBLIC_REGIME_EVENT_V1"
MIN_HISTORY = 1000
MIN_TRAIN = 500
MIN_HOLDOUT = 200
EMBARGO = MIN_BARS
MIN_GAIN = .04
MAX_EVALUATED_PER_CYCLE = 1
REGIMES = (
    "UPTREND", "DOWNTREND", "RANGING", "TRANSITION",
    "LOW_VOLATILITY", "HIGH_VOLATILITY", "IRREGULAR",
)
REASONS = frozenset(("PERIODIC", "PUBLIC_REGIME_CHANGE"))
SAFE_FALLBACK = "NO_VALIDATED_RESEARCH_MODEL"


class LearningError(ValueError):
    pass


def learning_enabled() -> bool:
    return (
        kb.enabled()
        and os.getenv("NEXORA_INTELLIGENCE_LEARNING_ENABLED", "false").lower() == "true"
    )


def promotion_enabled() -> bool:
    return learning_enabled() and (
        os.getenv("NEXORA_INTELLIGENCE_LEARNING_PROMOTION_ENABLED", "false").lower() == "true"
    )


def _utc(value):
    return kb._utc(value)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _valid_history(history: dict) -> tuple[list[str], list[int]]:
    if not isinstance(history, dict) or history.get("version") != SOURCE_VERSION:
        raise LearningError("Histórico I01/I03 público não confiável")
    if (history.get("data_quality") != "COMPLETE"
        or history.get("execution_allowed") is not False):
        raise LearningError("Aprendizado exige candles públicos fechados")
    timeframe = history.get("timeframe_seconds")
    if type(timeframe) is not int or timeframe not in market_data.SUPPORTED_GRANULARITIES:
        raise LearningError("Timeframe inválido")
    observations = history.get("observations")
    if not isinstance(observations, list) or len(observations) < MIN_HISTORY-MIN_BARS+1:
        raise LearningError("Amostra pública mínima ausente")
    epochs, states = [], []
    for observation in observations:
        if not isinstance(observation, dict):
            raise LearningError("Observação inválida")
        epoch, regime = observation.get("closed_epoch"), observation.get("regime")
        if type(epoch) is not int or regime not in REGIMES:
            raise LearningError("Regime fechado não verificável")
        epochs.append(epoch)
        states.append(regime)
    if any(b-a != timeframe for a,b in zip(epochs,epochs[1:])):
        raise LearningError("Lacuna, duplicidade ou série incompleta")
    if not all(0 < e <= history.get("observed_at_epoch", 0) for e in epochs):
        raise LearningError("Lookahead ou relógio incorreto")
    return states, epochs


async def collect_public_history(*, market: str, symbol: str,
                                 timeframe_seconds: int = 60,
                                 observed_at: int | None = None) -> dict:
    """One bounded call through I01's existing public candle client.

    No direct Deriv websocket/cache. I03's actual context classifier is
    evaluated for EACH historical closed 72-bar window without lookahead.
    """
    if observed_at is None:
        observed_at = int(time.time())
    window = await market_data.read_closed_window(
        symbol, timeframe_seconds, MIN_HISTORY, now=observed_at, timeout=8.0,
    )
    if (window.get("quality") != "COMPLETE"
        or window.get("usable_for_research") is not True
        or len(window.get("candles", [])) != MIN_HISTORY):
        raise LearningError("Histórico I01 insuficiente/desatualizado")
    observations = []
    bars = window["candles"]
    for end in range(MIN_BARS, len(bars)+1):
        subset = bars[end-MIN_BARS:end]
        closed_epoch = subset[-1]["epoch"]
        diagnostic = analyze_context({
            "quality": "COMPLETE", "usable_for_research": True,
            "closed_count": MIN_BARS, "last_closed_epoch": closed_epoch,
            "candles": subset,
        }, market=market, symbol=symbol,
            timeframe_seconds=timeframe_seconds,
            observed_at=closed_epoch+timeframe_seconds)
        if diagnostic["state"] == "INDETERMINATE":
            raise LearningError("Histórico I03 contém contexto indeterminado")
        observations.append({
            "closed_epoch": closed_epoch, "regime": diagnostic["state"]
        })
    return {
        "version": SOURCE_VERSION, "market":market, "symbol":symbol,
        "timeframe_seconds":timeframe_seconds,
        "observed_at_epoch":observed_at, "data_quality":"COMPLETE",
        "observations":observations, "context_version":CONTEXT_VERSION,
        "i01_last_closed_epoch":window["last_closed_epoch"],
        "execution_allowed":False,
    }


def _wilson_lower(successes: int, total: int) -> float:
    if total <= 0:
        return 0.
    z=1.959963984540054
    p=successes/total
    denominator=1+z*z/total
    center=p+z*z/(2*total)
    spread=z*math.sqrt((p*(1-p)+z*z/(4*total))/total)
    return max(0.,(center-spread)/denominator)


def _fit(states: list[str], last_training_index: int):
    transitions={s: Counter() for s in REGIMES}
    for idx in range(1,last_training_index):
        transitions[states[idx-1]][states[idx]] += 1
    # Laplace smoothing avoids undefined predictions for rare regimes.
    matrix = {
        prior: {next_state:transitions[prior][next_state]+1 for next_state in REGIMES}
        for prior in REGIMES
    }
    return matrix


def _predict(matrix, state):
    counts=matrix[state]
    # Stable deterministic tie-breaking; no random deployment decisions.
    return max(REGIMES,key=lambda candidate:(counts[candidate],-REGIMES.index(candidate)))


def evaluate_public_regimes(history: dict) -> dict:
    """Walk-forward, purged/embargoed, strict holdout research evaluation.

    No financial performance metric or order signal is generated.
    """
    states, epochs = _valid_history(history)
    n=len(states)
    cut=int(n*.64)
    holdout_start=cut+EMBARGO
    train_count=cut-1
    holdout_count=n-holdout_start
    if train_count < MIN_TRAIN or holdout_count < MIN_HOLDOUT:
        raise LearningError("Amostras treino/holdout insuficientes após embargo")
    matrix=_fit(states,cut)
    actual, predicted, baseline=[],[],[]
    for idx in range(holdout_start,n):
        actual.append(states[idx])
        predicted.append(_predict(matrix,states[idx-1]))
        baseline.append(states[idx-1])  # Persistence control, not a trade.
    wins=sum(a==b for a,b in zip(actual,predicted))
    basewins=sum(a==b for a,b in zip(actual,baseline))
    split=holdout_count//2
    halves=[
        {
            "model":sum(a==b for a,b in zip(actual[start:end],predicted[start:end])),
            "baseline":sum(a==b for a,b in zip(actual[start:end],baseline[start:end])),
            "n":end-start,
        }
        for start,end in ((0,split),(split,holdout_count))
    ]
    accuracy=wins/holdout_count
    baseline_accuracy=basewins/holdout_count
    lower=_wilson_lower(wins,holdout_count)
    # Strong gate: above naive no-change baseline, positive lift on BOTH
    # separated holdout halves, and conservative Wilson lower bound.
    eligible=bool(
        accuracy >= baseline_accuracy+MIN_GAIN
        and lower >= baseline_accuracy+.01
        and all((x["model"]-x["baseline"])/x["n"] >= .02 for x in halves)
    )
    artifact={
        "algorithm":"REGIME_MARKOV_LAPLACE_V1",
        "states":list(REGIMES), "transition_counts":matrix,
        "trained_through_epoch":epochs[cut-1],
        "embargo_bars":EMBARGO, "training_pairs":train_count,
        "source":SOURCE_VERSION, "context_version":CONTEXT_VERSION,
    }
    digest=_sha(artifact)
    return {
        "version":VERSION, "kind":"PUBLIC_MARKET_REGIME_RESEARCH",
        "market":history["market"], "symbol":history["symbol"],
        "timeframe_seconds":history["timeframe_seconds"],
        "observed_at_epoch":history["observed_at_epoch"],
        "period_from_epoch":epochs[0],
        "period_to_epoch":epochs[-1],
        "train_until_epoch":epochs[cut-1],
        "holdout_from_epoch":epochs[holdout_start],
        "embargo_bars":EMBARGO,
        "train_samples":train_count,
        "holdout_samples":holdout_count,
        "holdout_accuracy":round(accuracy,6),
        "baseline_accuracy":round(baseline_accuracy,6),
        "wilson95_lower":round(lower,6),
        "holdout_half_lifts":[
            round((x["model"]-x["baseline"])/x["n"],6) for x in halves
        ],
        "independent_holdout":True,
        "research_eligible":eligible,
        "reason_code": (
            "OOS_REGIME_LIFT_VERIFIED" if eligible
            else "INSUFFICIENT_OOS_LIFT_OVER_PERSISTENCE"
        ),
        "model_id_sha256":digest,
        "model_version":"i05_"+digest[:16],
        "artifact":artifact,
        "training_source":SOURCE_VERSION,
        "execution_allowed":False,
        "approved_for_trading":False,
    }


async def research_candidate(*, knowledge_id: int,
                             observed_at: int | None = None) -> dict:
    """Load I04 verified MARKET source and reuse I01/I03 directly.

    Must not train when source knowledge is invalid, expired, stale or
    mismatched with the latest closed market regime.
    """
    source=kb.get_knowledge(knowledge_id,now=datetime.fromtimestamp(
        observed_at if observed_at is not None else time.time(),tz=timezone.utc
    ))
    if source["category"]!="MARKET" or source["state"]!="ACTIVE":
        raise LearningError("Conhecimento I04 MARKET não está ativo")
    history=await collect_public_history(
        market=source["market"],symbol=source["symbol"],
        timeframe_seconds=source["timeframe_seconds"],
        observed_at=observed_at,
    )
    if history["observations"][-1]["regime"] not in REGIMES:
        raise LearningError("Regime público final inválido")
    now=datetime.fromtimestamp(history["observed_at_epoch"],tz=timezone.utc)
    source=kb.get_knowledge(
        knowledge_id,now=now,
        observed_regime=history["observations"][-1]["regime"])
    if not source["reusable_for_research"]:
        raise LearningError("I04 bloqueou treinamento por regime/validade")
    report=evaluate_public_regimes(history)
    report["knowledge_lineage"]={
        "market_knowledge_id":source["id"],
        "market_knowledge_revision":source["revision"],
        "market_knowledge_payload_sha256":source["payload_sha256"],
    }
    return report


def select_research_roles(*, candidate: dict, champion: dict | None = None,
                          fallback: dict | None = None) -> dict:
    """No financial signals: candidate/champion/fallback research arbitration.

    Without strong OOS validation the result is always safe fallback.
    Caller-provided champion/fallback require separately verified registry
    data; this pure function never claims they were deployed.
    """
    if not isinstance(candidate,dict) or candidate.get("version")!=VERSION:
        raise LearningError("Challenger I05 inválido")
    for item in (champion,fallback):
        if item is not None and (
            not isinstance(item,dict)
            or item.get("kind")!="PUBLIC_MARKET_REGIME_RESEARCH"
            or item.get("execution_allowed") is not False
        ):
            raise LearningError("Champion/fallback sem proveniência de pesquisa")
    challenger_eligible = candidate["research_eligible"] is True
    best = (candidate["model_id_sha256"] if challenger_eligible
            else champion.get("model_id_sha256") if champion
            else fallback.get("model_id_sha256") if fallback
            else SAFE_FALLBACK)
    return {
        "version":VERSION, "candidate":candidate["model_id_sha256"],
        "champion":champion.get("model_id_sha256") if champion else None,
        "challenger":candidate["model_id_sha256"],
        "fallback":fallback.get("model_id_sha256") if fallback else SAFE_FALLBACK,
        "research_recommendation":best,
        "promotion_requested":False, "promotion_performed":False,
        "reason_code":candidate["reason_code"],
        "execution_allowed":False, "approved_for_trading":False,
    }

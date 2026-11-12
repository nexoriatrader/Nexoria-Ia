"""Indicadores técnicos experimentais para exibição e simulação local.

Este módulo é somente analítico: não estima probabilidade de lucro, não gera
recomendações e não envia ordens à Deriv.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

MIN_SAMPLES = 55


def _ema(values: Sequence[float], period: int) -> float:
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _rsi(values: Sequence[float], period: int = 14) -> float:
    changes = [values[i] - values[i - 1] for i in range(len(values) - period, len(values))]
    gains = sum(max(change, 0.0) for change in changes) / period
    losses = sum(max(-change, 0.0) for change in changes) / period
    if losses == 0:
        return 50.0 if gains == 0 else 100.0
    return 100.0 - (100.0 / (1.0 + gains / losses))


def analyze_prices(prices: Sequence[float]) -> dict:
    """Return a conservative, explainable snapshot from a chronological tick series."""
    clean: list[float] = []
    for raw in prices:
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and value > 0:
            clean.append(value)

    if len(clean) < MIN_SAMPLES:
        return {
            "signal": "WAIT",
            "reason": f"Aguardando histórico suficiente ({len(clean)}/{MIN_SAMPLES} cotações)",
            "samples": len(clean),
            "indicators": None,
        }

    series = clean[-55:]
    current = series[-1]
    fast = _ema(series[-26:], 9)
    slow = _ema(series[-26:], 21)
    rsi = _rsi(series, 14)
    prior_window = series[-21:-1]
    prior_mean = sum(prior_window) / len(prior_window)
    variance = sum((value - prior_mean) ** 2 for value in prior_window) / len(prior_window)
    stddev = math.sqrt(variance)
    z_score = (current - prior_mean) / stddev if stddev > 0 else 0.0
    returns = [(series[i] / series[i - 1] - 1.0) * 100.0 for i in range(1, len(series))]
    recent_returns = returns[-20:]
    return_mean = sum(recent_returns) / len(recent_returns)
    volatility = math.sqrt(sum((value - return_mean) ** 2 for value in recent_returns) / len(recent_returns))
    momentum = (current / series[-20] - 1.0) * 100.0

    trend = None
    if fast > slow and momentum > 0:
        trend = "CALL"
    elif fast < slow and momentum < 0:
        trend = "PUT"

    prior_high = max(prior_window)
    prior_low = min(prior_window)
    breakout = "CALL" if current > prior_high else "PUT" if current < prior_low else None

    reversal = None
    if rsi <= 30 and z_score <= -1.5:
        reversal = "CALL"
    elif rsi >= 70 and z_score >= 1.5:
        reversal = "PUT"

    # Require two compatible, independent conditions; conflicts remain neutral.
    signal = "WAIT"
    reason = "Indicadores sem confirmação suficiente; aguardando nova leitura"
    if trend and breakout == trend:
        signal = trend
        reason = "Tendência e rompimento apontam na mesma direção"
    elif reversal and trend in (None, reversal) and breakout in (None, reversal):
        signal = reversal
        reason = "RSI e afastamento da média sugerem possível reversão"
    elif trend and breakout and trend != breakout:
        reason = "Sinais conflitantes entre tendência e rompimento"

    indicators = {
        "ema_fast": round(fast, 6),
        "ema_slow": round(slow, 6),
        "rsi_14": round(rsi, 2),
        "momentum_pct": round(momentum, 4),
        "volatility_pct": round(volatility, 4),
        "z_score": round(z_score, 3),
        "trend": trend,
        "breakout": breakout,
        "reversal": reversal,
    }
    return {
        "signal": signal,
        "reason": reason,
        "samples": len(clean),
        "indicators": indicators,
        "informational_only": True,
    }

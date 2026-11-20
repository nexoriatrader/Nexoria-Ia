"""I03 Context Engine: one public, deterministic, research-only intelligence.

Reuses validated I01 closed-candle windows, existing automation EMA/RSI
formulas and swing-structure pattern detection. No independent feed, model
registry, cache, table, background worker, credentials, orders, or user data.

The 'confidence' field is an explicit bounded HEURISTIC consistency measure,
NOT a calibrated win probability. No ML model is loaded without an existing
independently validated model artifact; I03 contains no such artifact.
"""
from __future__ import annotations

import math
from statistics import mean, median, pstdev

from ..automation_signals import _ema, _rsi
from ..trade_selector_v2 import swing_structure

VERSION = "intelligence2_context_v1"
MIN_BARS = 72
_BAD_QUALITY = frozenset(("INSUFFICIENT", "GAPPED", "STALE"))


def _indeterminate(window: dict | None, code: str, *, market: str,
                   symbol: str, timeframe_seconds: int, observed_at: int) -> dict:
    return {
        "version": VERSION, "source": "i01_public_closed_candles",
        "market": market, "symbol": symbol,
        "timeframe_seconds": timeframe_seconds, "observed_at_epoch": observed_at,
        "last_closed_epoch": (window or {}).get("last_closed_epoch"),
        "state": "INDETERMINATE", "direction": "NEUTRAL",
        "strength": 0.0, "stability": 0.0, "confidence": 0.0,
        "confidence_kind": "HEURISTIC_NOT_PROBABILISTIC",
        "reason_codes": [code], "evidence": {},
        "data_quality": (window or {}).get("quality", "UNAVAILABLE"),
        "research_usable": False,
        "ml": {"status": "NO_VALIDATED_MODEL_AVAILABLE", "used": False},
        "execution_allowed": False,
    }


def analyze_context(window: dict, *, market: str, symbol: str,
                    timeframe_seconds: int, observed_at: int) -> dict:
    """Describe current trend/regime from fully closed, continuous OHLC.

    I01 is the authoritative OHLC validator. This boundary verifies its
    declared validity and closed timestamps fail-closed, never fills gaps.
    Every indicator uses only the supplied *closed* bars, no lookahead.
    """
    if not isinstance(window, dict):
        return _indeterminate(None, "NO_VALID_I01_WINDOW", market=market,
                              symbol=symbol, timeframe_seconds=timeframe_seconds,
                              observed_at=observed_at)
    if window.get("quality") != "COMPLETE" or window.get("usable_for_research") is not True:
        return _indeterminate(window, "INVALID_OR_STALE_I01_WINDOW", market=market,
                              symbol=symbol, timeframe_seconds=timeframe_seconds,
                              observed_at=observed_at)
    bars = window.get("candles")
    expected = (observed_at // timeframe_seconds - 1) * timeframe_seconds
    if (not isinstance(bars, list) or len(bars) < MIN_BARS
        or window.get("last_closed_epoch") != expected
        or window.get("closed_count") != len(bars)):
        return _indeterminate(window, "INSUFFICIENT_OR_MISALIGNED_BARS", market=market,
                              symbol=symbol, timeframe_seconds=timeframe_seconds,
                              observed_at=observed_at)
    try:
        segment = bars[-MIN_BARS:]
        if any(type(b.get("epoch")) is not int for b in segment):
            raise ValueError("invalid epoch")
        if segment[-1]["epoch"] != expected or any(
            right["epoch"] - left["epoch"] != timeframe_seconds
            for left, right in zip(segment, segment[1:])
        ):
            raise ValueError("missing/unfinished bars")
        closes = [float(b["close"]) for b in segment]
        highs = [float(b["high"]) for b in segment]
        lows = [float(b["low"]) for b in segment]
        opens = [float(b["open"]) for b in segment]
        if (not all(math.isfinite(x) and x > 0 for x in closes + highs + lows + opens)
            or any(lo > min(op, cl) or hi < max(op, cl)
                   for lo, hi, op, cl in zip(lows, highs, opens, closes))):
            raise ValueError("invalid OHLC")
        fast = _ema(closes, 20)
        slow = _ema(closes, 50)
        rsi_levels = _rsi(closes, 14)
        if not fast or not slow or rsi_levels is None:
            raise ValueError("insufficient indicators")
        returns = [(b-a)/a for a,b in zip(closes,closes[1:])]
        vol = pstdev(returns)
        last30 = closes[-31:]
        travelled = sum(abs(b-a) for a,b in zip(last30,last30[1:]))
        efficiency = abs(last30[-1]-last30[0])/travelled if travelled else 0.0
        ema_gap = (fast[-1]-slow[-1])/closes[-1]
        trend_slope = (fast[-1]-fast[-5])/closes[-1]
        abs_gap = abs(ema_gap)
        ranges = [hi-lo for hi,lo in zip(highs,lows)]
        baseline = median(ranges[-31:-1])
        irregular = bool(
            baseline > 0 and ranges[-1] > 8*baseline
            and ranges[-1]/closes[-1] > 0.015
        )
        cross_recent = ((fast[-1]-slow[-1])*(fast[-5]-slow[-5]) <= 0)
        positive = ema_gap > 0 and trend_slope > 0 and closes[-1] >= fast[-1]
        negative = ema_gap < 0 and trend_slope < 0 and closes[-1] <= fast[-1]
        direction = "UP" if positive else "DOWN" if negative else "NEUTRAL"
        same_side = sum(1 for i in range(-12,0)
                        if (closes[i] >= fast[i] if direction == "UP"
                            else closes[i] <= fast[i] if direction == "DOWN"
                            else abs(closes[i]-fast[i]) < closes[i] * max(vol,1e-6)))
        stability = same_side / 12.0
        relative_gap = abs_gap / max(vol, 1e-7)
        strength = min(1.0, .6*efficiency + .4*min(1.0, relative_gap/2.0))
        # Reliability derived from stability/structure, never an estimated
        # probability of win or a model trained on user settlements.
        confidence = min(.95, .25 + .45*stability + .30*strength)
        if irregular:
            state = "IRREGULAR"
        elif vol <= 1e-8:
            state = "LOW_VOLATILITY"
        elif vol >= .012:
            state = "HIGH_VOLATILITY"
        elif cross_recent or (direction == "NEUTRAL" and efficiency >= .35):
            state = "TRANSITION"
        elif efficiency < .20 or relative_gap < .45:
            state = "RANGING"
        elif direction == "UP":
            state = "UPTREND"
        elif direction == "DOWN":
            state = "DOWNTREND"
        else:
            state = "TRANSITION"
        if state in ("IRREGULAR", "LOW_VOLATILITY", "RANGING"):
            direction = "NEUTRAL"
        research_usable = state != "IRREGULAR"
        pattern = ("HIGHER_HIGHS_HIGHER_LOWS" if swing_structure(segment, "CALL")
                   else "LOWER_HIGHS_LOWER_LOWS" if swing_structure(segment, "PUT")
                   else "NO_CONFIRMED_SWING_PATTERN")
        # Two consecutive confirmed closes supporting the current direction
        # are an explicit (weak) reversal evidence, NEVER alone sufficient.
        two_closes = (len(closes) >= 3 and
                      ((closes[-1] > closes[-2] > closes[-3]) if direction == "UP"
                       else (closes[-1] < closes[-2] < closes[-3]) if direction == "DOWN"
                       else False))
        reason = [state, "CLOSED_BARS_ONLY", "DETERMINISTIC_INDICATORS"]
        if pattern != "NO_CONFIRMED_SWING_PATTERN":
            reason.append(pattern)
        return {
            "version": VERSION, "source": "i01_public_closed_candles",
            "market": market, "symbol": symbol,
            "timeframe_seconds": timeframe_seconds, "observed_at_epoch": observed_at,
            "last_closed_epoch": window["last_closed_epoch"],
            "data_quality": "COMPLETE",
            "state": state, "direction": direction,
            "strength": round(strength, 5), "stability": round(stability, 5),
            "confidence": round(confidence, 5),
            "confidence_kind": "HEURISTIC_NOT_PROBABILISTIC",
            "evidence": {
                "ema20": round(fast[-1], 8), "ema50": round(slow[-1], 8),
                "ema_gap_fraction": round(ema_gap, 8),
                "ema_slope_fraction": round(trend_slope, 8),
                "rsi14": round(rsi_levels[-1], 4),
                "realized_volatility": round(vol, 8),
                "trend_efficiency": round(efficiency, 5),
                "swing_pattern": pattern,
                "recent_ema_cross": cross_recent,
                "two_closed_candles_confirm_direction": bool(two_closes),
                "closed_bars_used": len(segment),
                "last_closed_epoch": window["last_closed_epoch"],
            },
            "reason_codes": reason, "research_usable": research_usable,
            "ml": {"status": "NO_VALIDATED_MODEL_AVAILABLE", "used": False},
            "execution_allowed": False,
        }
    except (KeyError, ValueError, TypeError, IndexError, ArithmeticError, OverflowError):
        return _indeterminate(window, "INCONSISTENT_CONTEXT_INPUT",
                              market=market, symbol=symbol,
                              timeframe_seconds=timeframe_seconds,
                              observed_at=observed_at)

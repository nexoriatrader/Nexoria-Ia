"""Experimental, causal liquidity-sweep/CHoCH/FVG strategy for Deriv 1HZ100V.

This operationalizes the requested SMC concept; it is NOT a claim that synthetic
indices obey institutional liquidity mechanics. Only completed M1, M5, M15,
H1 candles are used. Entry is approximated at close of the retest candle.
No order placement and no broker authorization.
"""
from __future__ import annotations
from collections import deque

def _aggregate(m1, minutes):
    size = 60 * minutes
    batches = {}
    for row in m1:
        start = row[0] // size * size
        batches.setdefault(start, []).append(row)
    out = []
    for start in sorted(batches):
        values = batches[start]
        if len(values) != minutes:
            continue
        if any(values[j][0] != start + j*60 for j in range(minutes)):
            continue
        out.append((start, values[0][1], max(v[2] for v in values),
                    min(v[3] for v in values), values[-1][4]))
    return out


def _latest_closed(bars, t, minutes, lookback):
    """Only high-timeframe bars whose final minute has completed by t."""
    cutoff = t - (minutes - 1) * 60
    # Index advances through aligned bars, never using a forming HTF candle.
    idx = cutoff // (minutes * 60)
    # The caller uses dictionary lookups for predictable and cheap access.
    return [bars[k] for k in range(idx - lookback + 1, idx + 1) if k in bars]


def _order_blocks(closed_bars):
    """Last opposite-body candle before a large impulse, using *only* closed HTF."""
    if len(closed_bars) < 6:
        return []
    bodies = [abs(b[4]-b[1]) for b in closed_bars]
    blocks = []
    for i in range(5, len(closed_bars)):
        prev, impulse = closed_bars[i-1], closed_bars[i]
        baseline = sum(bodies[i-5:i]) / 5
        if baseline <= 0 or bodies[i] < 1.5 * baseline:
            continue
        # Bullish displacement preceded by bearish candle => demand order block.
        if prev[4] < prev[1] and impulse[4] > impulse[1] and impulse[4] > prev[2]:
            blocks.append((1, min(prev[1],prev[4]), max(prev[1],prev[4])))
        # Bearish displacement preceded by bullish candle => supply order block.
        elif prev[4] > prev[1] and impulse[4] < impulse[1] and impulse[4] < prev[3]:
            blocks.append((-1, min(prev[1],prev[4]), max(prev[1],prev[4])))
    return blocks[-4:]



def make_smc_signals(m1, signal_minutes=1):
    """Return sparse causal signals keyed by M1 closed-minute timestamps.

    Rules are intentionally fixed before validation:
    1) zone: last 20 closed M15 bars or last 8 closed H1 bars' extrema;
    2) sweep: wick beyond recent 10-bar local extreme, closes inside it;
    3) CHoCH: within 8 bars close through pre-sweep 3-bar internal structure;
    4) FVG (three completed bars) or impulse 50% retracement;
    5) after CHoCH, retest within 10 bars with directional closing confirmation.
    """
    if signal_minutes not in (1, 5):
        raise ValueError("SMC triggers support M1 or M5 only")
    if len(m1) < 24*60:
        return {}
    trigger = _aggregate(m1, signal_minutes)
    m15 = {bar[0] // 900: bar for bar in _aggregate(m1, 15)}
    h1 = {bar[0] // 3600: bar for bar in _aggregate(m1, 60)}
    signals = {}
    pending = None
    confirmed = None
    cooldown_until = -1

    for i in range(30, len(trigger)):
        current = trigger[i]
        t, op, high, low, close = current
        if t < cooldown_until:
            continue
        prior = trigger[i-10:i]
        internal = trigger[i-3:i]
        # Trigger candle's close epoch; no use of a later price.
        close_epoch = t + (signal_minutes-1)*60
        m15_closed = _latest_closed(m15, close_epoch, 15, 20)
        h1_closed = _latest_closed(h1, close_epoch, 60, 8)
        if len(m15_closed) < 12 or len(h1_closed) < 5:
            continue
        zone_levels = [min(b[3] for b in m15_closed), max(b[2] for b in m15_closed),
                       min(b[3] for b in h1_closed), max(b[2] for b in h1_closed)]
        recent_high = max(x[2] for x in prior)
        recent_low = min(x[3] for x in prior)
        width = max(recent_high-recent_low, abs(close)*1e-7)
        tolerance = width * .35

        # Confirmed setup must get a retest only on subsequent bars.
        if confirmed:
            if i > confirmed["expiry"]:
                confirmed = None
            elif i > confirmed["at"]:
                side = confirmed["side"]
                gap = confirmed["gap"]
                middle = confirmed["mid"]
                touch_gap = gap is not None and low <= gap[1] and high >= gap[0]
                touch_mid = low <= middle <= high
                if (touch_gap or touch_mid) and (
                    (side == 1 and close > op and close > middle) or
                    (side == -1 and close < op and close < middle)):
                    signals[close_epoch] = side
                    cooldown_until = t + 10*signal_minutes*60
                    confirmed = None
                    pending = None
                    continue

        # Pending reversal after sweep; CHoCH must occur *after* sweep.
        if pending:
            if i > pending["expiry"]:
                pending = None
            elif i > pending["at"]:
                side = pending["side"]
                crossed = (side == 1 and close > pending["structure"]) or (
                    side == -1 and close < pending["structure"])
                if crossed:
                    anchor = pending["anchor"]
                    middle = (anchor + close)/2
                    gap = None
                    # Directional three-bar fair-value gap (if any).
                    if i >= 2:
                        a = trigger[i-2]
                        if side == 1 and a[2] < low:
                            gap = (a[2], low)
                        elif side == -1 and a[3] > high:
                            gap = (high, a[3])
                    confirmed = {"side":side, "at":i, "expiry":i+10,
                                 "mid":middle, "gap":gap}
                    pending = None
                    continue

        # Sweep of a previous local extreme at a completed H1/M15 zone.
        # Require a wick at least 1.5x the real body, not the first touch.
        body = max(abs(close-op), abs(close)*1e-9)
        lower_wick = min(op, close)-low
        upper_wick = high-max(op, close)
        order_blocks = _order_blocks(m15_closed) + _order_blocks(h1_closed)
        near_low = any(abs(low-z) <= tolerance for z in zone_levels[::2]) or any(
            side == 1 and band_low-tolerance <= low <= band_high+tolerance
            for side, band_low, band_high in order_blocks)
        near_high = any(abs(high-z) <= tolerance for z in zone_levels[1::2]) or any(
            side == -1 and band_low-tolerance <= high <= band_high+tolerance
            for side, band_low, band_high in order_blocks)
        if low < recent_low and close > recent_low and lower_wick >= 1.5*body and near_low:
            pending = {"side":1, "at":i, "expiry":i+8,
                       "structure":max(x[2] for x in internal), "anchor":low}
        elif high > recent_high and close < recent_high and upper_wick >= 1.5*body and near_high:
            pending = {"side":-1, "at":i, "expiry":i+8,
                       "structure":min(x[3] for x in internal), "anchor":high}
    return signals, {
        "name":"smc_sweep_choch_fvg_50",
        "zone_higher_timeframes":["M15","H1"],
        "entry_trigger_timeframe":f"M{signal_minutes}",
        "rules":"HTF extrema and opposite-candle order-block zones before >=1.5x body displacement; wick sweep of 10-bar local extremum; close through 3-bar structure in <=8 bars; FVG or 50%-impulse retest <=10 bars; entry at closed retest bar",
        "limitations":"Mechanical order-block proxy and historical price simulations; no assertion of algorithmic liquidity-seeking."
    }

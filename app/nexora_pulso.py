"""NEXORA PULSO v1: a falsifiable, read-only original market-regime hypothesis.

Research only: M1 historical Deriv 1HZ100V candles, three fixed signal
families, four time expiries, chronological 60/20/20 split. All features use
closed candles. Out-of-sample is exploratory because this historical window
has already been used in other experiments. No orders, tokens or APIs.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .m1_history import _read_existing, SYMBOL
from .multi_timeframe_study import score

CANDLES_PER_MINUTE = 1
EXPIRIES = (1, 3, 5, 15)
FAMILIES = ("efficient_pressure", "compression_release", "failed_sweep")
BREAK_EVEN_AT_88 = 1/1.88


def pulse_signals(rows):
    """Return decisions at closed minute t, plus diagnostic event counts.

    New indicator dimensions:
    * directional efficiency: absolute net 12m displacement / 12m path;
    * closing-location pressure: 5m range-normalized body positioning;
    * volatility state: 8m mean true range / 30m mean true range;
    * local break & rejection, all from previous completed bars.
    Gaps trigger a 90-minute burn-in and never create synthetic candles.
    """
    names = {family: [0] * len(rows) for family in FAMILIES}
    counts = {family: {"call": 0, "put": 0} for family in FAMILIES}
    n = len(rows)
    for i in range(90, n):
        window = rows[i-90:i+1]
        if any(window[k][0] - window[k-1][0] != 60 for k in range(1, len(window))):
            continue
        recent = rows[i-30:i+1]
        curr = recent[-1]
        c = curr[4]
        minute_ranges = [max(x[2]-x[3], 1e-10) for x in recent]
        changes = [recent[k][4] - recent[k-1][4] for k in range(1, len(recent))]
        mean_range = sum(minute_ranges[-30:-1]) / 29
        if mean_range <= 0:
            continue
        path12 = sum(abs(x) for x in changes[-12:])
        net12 = sum(changes[-12:])
        efficiency = abs(net12) / max(path12, 1e-10)
        direction = 1 if net12 > 0 else -1 if net12 < 0 else 0
        def clv(x):
            rng = max(x[2]-x[3], 1e-10)
            return (2*x[4] - x[2] - x[3]) / rng
        pressure5 = sum(clv(x) for x in recent[-5:]) / 5
        vol8 = sum(minute_ranges[-8:]) / 8 / mean_range
        prior12 = rows[i-12:i]
        prior20 = rows[i-20:i]
        prev_8 = rows[i-8:i]
        high12 = max(x[2] for x in prior12)
        low12 = min(x[3] for x in prior12)
        high20 = max(x[2] for x in prior20)
        low20 = min(x[3] for x in prior20)
        candle_impulse = (curr[4]-curr[1]) / max(curr[2]-curr[3], 1e-10)

        # Family A: persistent price path with strong location pressure,
        # avoiding unusually amplified micro-volatility.
        if (direction and efficiency >= .43 and .65 <= vol8 <= 1.75
                and direction * pressure5 >= .28
                and direction * candle_impulse >= .50
                and direction * changes[-1] >= .32 * mean_range):
            names["efficient_pressure"][i] = direction

        # Family B: yesterday's "squeeze-breakout" analogy at minute scale.
        # Requires compressed 8-minute ranges, then a break on a forceful bar.
        past_vol8 = sum(max(x[2]-x[3], 1e-10) for x in prev_8) / 8 / mean_range
        if past_vol8 <= .76 and minute_ranges[-1] >= 1.25 * mean_range:
            if c > high12 and candle_impulse >= .65:
                names["compression_release"][i] = 1
            elif c < low12 and candle_impulse <= -.65:
                names["compression_release"][i] = -1

        # Family C: failed excursion of a 20-bar extreme with rejection wick;
        # avoid strong directional regimes, where countertrend is hazardous.
        wick_low = min(curr[1], curr[4]) - curr[3]
        wick_high = curr[2] - max(curr[1], curr[4])
        if efficiency < .34 and vol8 <= 1.7:
            if curr[3] < low20 and c > low20 and wick_low >= .5 * minute_ranges[-1] and clv(curr) > .20:
                names["failed_sweep"][i] = 1
            elif curr[2] > high20 and c < high20 and wick_high >= .5 * minute_ranges[-1] and clv(curr) < -.20:
                names["failed_sweep"][i] = -1

        for name in FAMILIES:
            sign = names[name][i]
            if sign:
                counts[name]["call" if sign == 1 else "put"] += 1
    return names, counts


def research(rows):
    if len(rows) < 43000:
        raise ValueError("Less than 43000 recorded M1 candles")
    rows = sorted(rows, key=lambda x: x[0])
    if any(b[0] <= a[0] for a,b in zip(rows,rows[1:])):
        raise ValueError("Non-increasing M1 candle times")
    first, end = rows[0][0], rows[-1][0]+60
    expected = (end-first)//60
    missing = expected-len(rows)
    if missing < 0 or missing > max(30, math.floor(expected*.001)):
        raise ValueError("Historical gaps too large to trust")
    timeline = {row[0]:row[4] for row in rows}
    t60 = first + (expected*6//10)*60
    t80 = first + (expected*8//10)*60
    signals, events = pulse_signals(rows)
    results = []
    for name in FAMILIES:
        for expiry in EXPIRIES:
            direction = signals[name]
            a = score(rows, direction, timeline, expiry, first, t60)
            b = score(rows, direction, timeline, expiry, t60, t80)
            c = score(rows, direction, timeline, expiry, t80, end)
            results.append({
                "family": name, "expiry_minutes": expiry,
                "discovery_60pct": a, "selection_20pct": b,
                "final_20pct": c,
            })
    # FIXED selection rule: require >=40 signals in both development
    # partitions, positive profit per trade in both, then rank by the
    # worse normalized expectancy. Never use the final 20% in selection.
    eligible = [
        r for r in results
        if r["discovery_60pct"]["signals"] >= 40
        and r["selection_20pct"]["signals"] >= 40
        and r["discovery_60pct"]["pnl_per_1_usd_at_88pct"] > 0
        and r["selection_20pct"]["pnl_per_1_usd_at_88pct"] > 0
    ]
    def robustness(r):
        a, b = r["discovery_60pct"], r["selection_20pct"]
        return min(
            a["pnl_per_1_usd_at_88pct"]/a["signals"],
            b["pnl_per_1_usd_at_88pct"]/b["signals"])
    selected = max(eligible, key=robustness) if eligible else None
    all_results = {
        "state": "RESEARCH_ONLY", "study": "NEXORA_PULSO_V1",
        "symbol": SYMBOL,
        "period_from":datetime.fromtimestamp(first,timezone.utc).isoformat(),
        "period_to":datetime.fromtimestamp(end,timezone.utc).isoformat(),
        "candles": len(rows), "missing_minutes": missing,
        "coverage_pct": round(100*len(rows)/expected,5),
        "split": "60% discovery / 20% candidate selection / 20% sealed evaluation",
        "families": list(FAMILIES), "expiries": list(EXPIRIES),
        "combinations":len(results), "event_counts":events,
        "candidates_eligible":len(eligible), "chosen_before_final": {
            "family": selected["family"], "expiry_minutes":selected["expiry_minutes"],
            "discovery":selected["discovery_60pct"],
            "selection":selected["selection_20pct"],
            "final":selected["final_20pct"],
        } if selected else None,
        "results":results,
        "payout_per_win_hypothetical": .88,
        "break_even_at_88":round(BREAK_EVEN_AT_88,5),
        "orders_sent":0,"execution_enabled":False,
        "martingale":False,"cycles":False,
        "notes":"This is a novel exploratory hypothesis, not a validated live strategy. Even sealed chronological final data are drawn from a previously studied month. Expiry is approximated with closing prices, not actual Deriv order execution or settlement; historical payouts unknown. Testing many hypotheses inflates false positives. No claim of predictive accuracy or guaranteed profit."
    }
    return all_results


def run_stored_pulso():
    candles = _read_existing()
    return research([candles[t] for t in sorted(candles)])

"""NEXORA PULSO v2: diagnose directional and volatility asymmetry.

Research-only fixed hypotheses; new strategies are not authorized for the
AutoPilot. Tests are chronological and use Deriv M1 candles already persisted.
Do not use last 20% for choosing parameters or the direction to trade.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import sqrt
from .m1_history import _read_existing
from .multi_timeframe_study import score
from .nexora_pulso import pulse_signals, BREAK_EVEN_AT_88

EXPIRIES=(3,5,15)
# Deliberately small, pre-declared search:
# baseline, volatility-comfort, confirm-trend, oppose-trend x CALL/PUT
REGIMES=("baseline","stable_volatility","aligned_20m","counter_20m")
DIRECTIONS=("CALL","PUT")


def make_variants(rows):
    original,_=pulse_signals(rows)
    pressure=original["efficient_pressure"]
    result={(reg,side):[0]*len(rows) for reg in REGIMES for side in DIRECTIONS}
    for i in range(110,len(rows)):
        direction=pressure[i]
        if not direction:
            continue
        window=rows[i-30:i+1]
        # Strict 30-minute uninterrupted feature windows.
        if any(window[j][0]-window[j-1][0]!=60 for j in range(1,len(window))):
            continue
        sign=1 if direction>0 else -1
        side="CALL" if sign>0 else "PUT"
        ranges=[max(b[2]-b[3],1e-10) for b in window]
        long_range=sum(ranges[:-1])/30
        recent_range=sum(ranges[-8:])/8
        volatility_ratio=recent_range/max(long_range,1e-10)
        trend20=window[-1][4]-window[-21][4]
        result[("baseline",side)][i]=sign
        # Range has not expanded or contracted drastically.
        if .80<=volatility_ratio<=1.20:
            result[("stable_volatility",side)][i]=sign
        if sign*trend20>0:
            result[("aligned_20m",side)][i]=sign
        elif sign*trend20<0:
            result[("counter_20m",side)][i]=sign
    return result


def report():
    data=_read_existing()
    rows=[data[t] for t in sorted(data)]
    if len(rows)<43000:
        raise ValueError("Historical M1 series insufficient")
    first=rows[0][0];end=rows[-1][0]+60
    expected=(end-first)//60
    missing=expected-len(rows)
    if missing<0 or missing>max(30,int(expected*.001)):
        raise ValueError("Historical gaps exceed 0.1%")
    closes={b[0]:b[4] for b in rows}
    cut60=first+(expected*60//100)*60
    cut80=first+(expected*80//100)*60
    variants=make_variants(rows)
    models=[]
    for (regime,side),sig in variants.items():
        for expiry in EXPIRIES:
            a=score(rows,sig,closes,expiry,first,cut60)
            b=score(rows,sig,closes,expiry,cut60,cut80)
            c=score(rows,sig,closes,expiry,cut80,end)
            models.append({"regime":regime,"side":side,"expiry":expiry,
                           "train":a,"selection":b,"final":c})
    def net(r):
        return r["pnl_per_1_usd_at_88pct"]/max(r["signals"],1)
    # Pre-committed: require ample samples and positive expectancy in both
    # development slices. The final slice is not consulted until selection.
    eligible=[r for r in models if
              r["train"]["signals"]>=80 and r["selection"]["signals"]>=35
              and net(r["train"])>0 and net(r["selection"])>0]
    eligible.sort(key=lambda r:min(net(r["train"]),net(r["selection"])),reverse=True)
    chosen=eligible[0] if eligible else None
    return {
        "state":"RESEARCH_ONLY","study":"NEXORA_PULSO_V2",
        "period_from":datetime.fromtimestamp(first,timezone.utc).isoformat(),
        "period_to":datetime.fromtimestamp(end,timezone.utc).isoformat(),
        "m1_candles":len(rows),"missing_minutes":missing,
        "coverage_pct":round(100*len(rows)/expected,5),
        "models_tested":len(models),
        "break_even_at_88":round(BREAK_EVEN_AT_88,5),
        "split":"60/20/20 chronological",
        "selected_from_first_80pct":{
            "regime":chosen["regime"],"side":chosen["side"],
            "expiry":chosen["expiry"],
            "train":chosen["train"],"selection":chosen["selection"],
            "final":chosen["final"]} if chosen else None,
        "eligible_models":len(eligible),
        "models":models,"orders_sent":0,"execution_enabled":False,
        "note":"Exploratory testing on a previously inspected historical month, NOT independent future validation. Closing-price proxy at hypothetical 88% payout. No claim of edge, no order execution, no Martingale."
    }

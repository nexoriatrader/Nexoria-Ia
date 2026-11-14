"""Owner-only exploratory EMA20/EMA50 + RSI14 pullback study on 1HZ100V.

Strictly read-only. All signals use finished M1 or M3 candles.
Rise/Fall expiry 3 and 5 minutes uses closing-price approximations;
Deriv contracts are NOT settled here, and Higher/Lower/tick contracts
are deliberately not conflated with fixed-duration Rise/Fall.
"""
from __future__ import annotations
from datetime import datetime, timezone
from math import sqrt
from .multi_timeframe_study import aggregate, score, MINUTE, SYMBOL, PAYOUT


def _signals(bars):
    n = len(bars)
    decisions = [0] * n
    if n < 100:
        return decisions
    closes = [r[4] for r in bars]
    e20, e50 = closes[0], closes[0]
    previous_e20 = previous_e50 = closes[0]
    # Wilder RSI uses properly seeded 14-bar average price differences.
    gain = loss = 0.0
    rsis = [None] * n
    ema20s = [None] * n
    ema50s = [None] * n
    atrs = [None] * n
    tr_queue = []
    last_gap = 0
    bar_seconds = (bars[1][0] - bars[0][0])
    for i in range(1, n):
        if bars[i][0] - bars[i-1][0] != bar_seconds:
            last_gap = i
            # Indicators must be fully re-warmed on a continuous segment.
            e20 = e50 = closes[i]
            gain = loss = 0.0
            tr_queue.clear()
            continue
        previous_e20, previous_e50 = e20, e50
        e20 += 2/21 * (closes[i]-e20)
        e50 += 2/51 * (closes[i]-e50)
        ema20s[i] = e20
        ema50s[i] = e50
        change = closes[i]-closes[i-1]
        if i-last_gap <= 14:
            gain += max(change, 0.0)
            loss += max(-change, 0.0)
            if i-last_gap == 14:
                gain /= 14
                loss /= 14
        else:
            gain = (gain*13 + max(change, 0.0))/14
            loss = (loss*13 + max(-change, 0.0))/14
        if i-last_gap >= 14:
            rsis[i] = 100*gain/(gain+loss) if gain+loss > 0 else 50.0
        b = bars[i]
        tr = max(b[2]-b[3], abs(b[2]-closes[i-1]), abs(b[3]-closes[i-1]))
        tr_queue.append(tr)
        if len(tr_queue) > 14:
            tr_queue.pop(0)
        if len(tr_queue)==14:
            atrs[i] = sum(tr_queue)/14
        if i-last_gap < 60 or i < 3:
            continue
        prior = bars[i-1]
        before = bars[i-2]
        rsi = rsis[i]
        atr = atrs[i]
        if not atr or rsi is None:
            continue
        op, high, low, close = b[1:]
        body = abs(close-op)
        # Large-bodied confirmation, not a doji. ATR normalizes across prices.
        strength = body >= .45*atr
        # Pullback must approach EMA20 on the immediately preceding candle.
        eprev = ema20s[i-1]
        ebefore = ema20s[i-2]
        approaching = (eprev is not None and ebefore is not None and
                       abs(prior[4]-eprev) <= abs(before[4]-ebefore) + .10*atr)
        # A touch of EMA20 or proximity within 15% ATR is allowed.
        touch = low <= e20+.15*atr and high >= e20-.15*atr
        bull_wick = min(op,close)-low
        bear_wick = high-max(op,close)
        bull = (e20 > e50 and e20 > previous_e20 and e50 > previous_e50 and
                approaching and touch and strength and close > op and close >= e20 and
                bull_wick >= .15*body and 50 < rsi < 75 and
                prior[4] >= eprev - .4*atr)
        bear = (e20 < e50 and e20 < previous_e20 and e50 < previous_e50 and
                approaching and touch and strength and close < op and close <= e20 and
                bear_wick >= .15*body and 25 < rsi < 50 and
                prior[4] <= eprev + .4*atr)
        if bull:
            decisions[i] = 1
        elif bear:
            decisions[i] = -1
    return decisions


def study_from_saved_m1(m1):
    if not m1 or len(m1)<43000:
        raise ValueError("Historical M1 coverage too small")
    first, last = m1[0][0], m1[-1][0]
    expected = (last-first)//MINUTE+1
    missing = expected-len(m1)
    if missing<0 or missing>max(30,int(expected*.001)):
        raise ValueError("Historical M1 gaps exceed 0.1%")
    closes = {r[0]:r[4] for r in m1}
    split = first + (((last+MINUTE-first)*7//10)//MINUTE)*MINUTE
    out = []
    for tf in (1,3):
        bars=aggregate(m1,tf)
        directions=_signals(bars)
        for expiry in (3,5):
            train=score(bars,directions,closes,expiry,first,split)
            holdout=score(bars,directions,closes,expiry,split,last+MINUTE)
            out.append({"timeframe":f"M{tf}","expiry_minutes":expiry,
                        "strategy":"ema20_ema50_rsi14_pullback",
                        "training":train,"holdout":holdout})
    return {
        "state":"RESEARCH_ONLY", "symbol":SYMBOL,
        "strategy":"ema20_ema50_rsi14_pullback",
        "rules_version":"V1_FIXED_BEFORE_RESULT",
        "period_from":datetime.fromtimestamp(first,timezone.utc).isoformat(),
        "period_to":datetime.fromtimestamp(last+MINUTE,timezone.utc).isoformat(),
        "m1_candles":len(m1),"missing_minutes":missing,
        "coverage_pct":round(100*len(m1)/expected,5),
        "split":"70% chronological training / 30% chronological holdout",
        "rules":{
            "ema_fast":20,"ema_slow":50,"rsi_period":14,
            "bull_rsi":"50 < RSI < 75", "bear_rsi":"25 < RSI < 50",
            "slope":"both EMAs positive for Rise, negative for Fall",
            "pullback":"previous bar moved toward EMA20; prior close within 0.4 ATR",
            "touch":"confirmation candle range within 0.15 ATR of EMA20",
            "confirmation":"close on correct side EMA20; body >=0.45 ATR; rejection wick >=0.15 body",
            "non_overlap":"one simulated contract at a time; skip gap-affected paths"
        },
        "results":out,"payout_assumed":PAYOUT,
        "payout_actual_verified":False,"orders_sent":0,"execution_enabled":False,
        "warning":"Rise/Fall directional bar-close proxy, not actual Deriv executions or Higher/Lower contracts. 200-300 ticks is not equivalent to 3-5 minutes. 88% win payout illustrative; no real money, Martingale or cycles. This additional model was proposed after inspecting the same historical window; holdout is exploratory, not independent."
    }


def run_stored_pullback_study():
    from .m1_history import _read_existing
    candles=_read_existing()
    return study_from_saved_m1([candles[t] for t in sorted(candles)])

"""Live DEMO-only NEXORA PULSO v2 signal verification.

Mirrors the historical PULSO v2 hypothesis on the 1HZ100V original
Deriv M1 closed candles. For user-opted-in DEMO only, not real-money
and never places orders. Fail closed if data or contract support missing.
"""
from __future__ import annotations
import asyncio
import time
from .market import MarketError, public_request
from .trade_selector_v2 import strict_closed_bars, supported
from .nexora_pulso_v2 import make_variants

SYMBOL="1HZ100V"
STRATEGY="nexora_pulso_v2_stable_call_5m"
_cache={"at":0.0,"last_closed":None,"candidate":None}
_cache_lock=asyncio.Lock()


async def confirmed_pulso_signal():
    now=time.time()
    # A decision is relevant only during the beginning of the NEXT M1 candle.
    # Cache by 60-second boundary to prevent repeated expensive Deriv queries.
    if now % 60 > 12:
        return None
    async with _cache_lock:
        now=time.time()
        bucket=int(now//60)*60
        if _cache["last_closed"]==bucket:
            return dict(_cache["candidate"]) if _cache["candidate"] else None
        _cache.update({"last_closed":bucket,"candidate":None})
        if not await supported(SYMBOL,minutes=5):
            return None
        raw=await public_request({
            "ticks_history":SYMBOL,"style":"candles",
            "granularity":60,"count":165,"end":"latest"
        },expected="candles",timeout=18)
        data=strict_closed_bars(raw,60,now=time.time())
        if len(data)<135:
            return None
        latest=data[-1]
        # The last completed minute must end at this exact current boundary.
        if latest["epoch"]+60 != bucket:
            return None
        # Historical V2 must see >110 closed bars: its loop begins
        # at index 110. Truncating to 110 made every live signal zero.
        tail=data[-135:]
        if any(tail[i]["epoch"]-tail[i-1]["epoch"]!=60 for i in range(1,len(tail))):
            return None
        rows=[[x["epoch"],x["open"],x["high"],x["low"],x["close"]] for x in tail]
        choices=make_variants(rows)
        signal=choices[("stable_volatility","CALL")][-1]
        if signal!=1:
            return None
        # Not a static replay: only a live closed candle can create a candidate.
        candidate={
            "strategy":STRATEGY,"direction":"CALL","symbol":SYMBOL,
            "minutes":5,"bar_seconds":60,
            "last_bar_epoch":latest["epoch"],
            "reason":"PULSO v2: pressão direcional e volatilidade estável; DEMO M1",
            "score":1,
        }
        _cache["candidate"]=candidate
        return dict(candidate)

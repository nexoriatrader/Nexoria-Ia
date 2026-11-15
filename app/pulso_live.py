"""Live DEMO-only NEXORA PULSO v2 signal verification.

Mirrors the historical PULSO v2 hypothesis on the 1HZ100V original
Deriv M1 closed candles. For user-opted-in DEMO only, not real-money
and never places orders. Fail closed if data or contract support missing.
"""
from __future__ import annotations
import asyncio
import os
import time
import logging
from .market import MarketError, public_request
from .trade_selector_v2 import strict_closed_bars, supported, catalogue
from .nexora_pulso_v2 import make_variants

SYMBOL="1HZ100V"
STRATEGY="nexora_pulso_v2_stable_call_5m"
# Rollback without code edits: NEXORA_PULSO_SYMBOLS_MODE=single
SYMBOLS_MODE=os.getenv("NEXORA_PULSO_SYMBOLS_MODE","all").strip().lower()
_log=logging.getLogger("nexora.pulso")
_cache={"last_bucket":None,"candidate":None}
_cache_lock=asyncio.Lock()


async def _candidate_for_symbol(symbol, bucket, *, deadline):
    """Only a closed broker-provided M1 candle can authorize a DEMO signal."""
    if time.time()>=deadline:
        return None
    if not await supported(symbol, minutes=5):
        return None
    # Historical signal warm-up requires >110 bars. Do not infer gaps.
    raw=await public_request({
        "ticks_history":symbol,"style":"candles",
        "granularity":60,"count":165,"end":"latest"
    },expected="candles",timeout=8)
    bars=strict_closed_bars(raw,60,now=time.time())
    if len(bars)<135 or bars[-1]["epoch"]+60!=bucket:
        return None
    tail=bars[-135:]
    if any(tail[j]["epoch"]-tail[j-1]["epoch"]!=60 for j in range(1,len(tail))):
        return None
    rows=[[b["epoch"],b["open"],b["high"],b["low"],b["close"]]
          for b in tail]
    direction=make_variants(rows)[("stable_volatility","CALL")][-1]
    if direction!=1 or time.time()>=deadline:
        return None
    return {
        "strategy":STRATEGY,"direction":"CALL","symbol":symbol,
        "minutes":5,"bar_seconds":60,
        "last_bar_epoch":bars[-1]["epoch"],
        "reason":"PULSO V2: pressão eficiente + volatilidade estável; CALL M1/5min",
        "score":1,
    }


async def confirmed_pulso_signal():
    """Scan all Deriv-listed Volatility symbols (or single-symbol fallback).

    Return at most ONE live candidate per closed M1 bar. Fail closed on
    incomplete data, closed execution window, unsupported duration or errors.
    Existing DEMO reconciliation and official risk gates remain in main.py.
    """
    now=time.time()
    if now%60>12:
        return None
    async with _cache_lock:
        now=time.time()
        bucket=int(now//60)*60
        if _cache["last_bucket"]==bucket:
            return dict(_cache["candidate"]) if _cache["candidate"] else None
        _cache.update(last_bucket=bucket,candidate=None)
        deadline=bucket+12
        if SYMBOLS_MODE=="single":
            symbols=[SYMBOL]
        else:
            # Broker's current catalog, not fabricated static symbols.
            instruments=await asyncio.wait_for(catalogue("volatility"),timeout=5)
            symbols=sorted({i["symbol"] for i in instruments})
            if not symbols:
                _log.warning("PULSO market catalogue empty; no DEMO signals")
                return None
        sem=asyncio.Semaphore(5)
        async def examine(symbol):
            async with sem:
                try:
                    return await _candidate_for_symbol(symbol,bucket,deadline=deadline)
                except (MarketError,TimeoutError,OSError,TypeError,ValueError) as exc:
                    _log.info("PULSO M1 ignored symbol=%s error_type=%s",symbol,type(exc).__name__)
                    return None
        # Execute bounded asynchronous calls, never beyond candle entry window.
        remaining=deadline-time.time()
        if remaining<=1:
            return None
        try:
            results=await asyncio.wait_for(
                asyncio.gather(*(examine(symbol) for symbol in symbols)),
                timeout=max(.5,remaining-0.4))
        except TimeoutError:
            _log.warning("PULSO M1 scan deadline exceeded; skip candle")
            return None
        candidates=[item for item in results if item]
        if candidates and time.time()<deadline:
            # Rotate fair ordering by minute; never turn historical results
            # from 1HZ100V into claims about other assets.
            candidates.sort(key=lambda x:x["symbol"])
            chosen=candidates[(bucket//60)%len(candidates)]
            _cache["candidate"]=chosen
            _log.info("PULSO ready: eligible_symbols=%s signals=%s chosen=%s",
                      len(symbols),len(candidates),chosen["symbol"])
            return dict(chosen)
        return None

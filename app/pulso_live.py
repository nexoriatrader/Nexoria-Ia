"""Live NEXORA PULSO v2 signal verification.

Mirrors the PULSO v2 hypothesis on Deriv M1 closed candles. This module only
produces signal candidates; DEMO or explicitly opted-in REAL runtimes apply
their own broker, account, consent, and risk checks before any order. Fail
closed if data or contract support is missing. This rule does not promise profit.
"""
from __future__ import annotations
import asyncio
import os
import time
import logging
from .market import MarketError, public_request, shared_candle_request
from .trade_selector_v2 import strict_closed_bars, supported, catalogue
from .nexora_pulso_v2 import make_variants

SYMBOL="1HZ100V"
STRATEGY="nexora_pulso_v2_stable_call_5m"
# Rollback without code edits: NEXORA_PULSO_SYMBOLS_MODE=single
SYMBOLS_MODE=os.getenv("NEXORA_PULSO_SYMBOLS_MODE","all").strip().lower()
_log=logging.getLogger("nexora.pulso")
_cache={}
_cache_lock=asyncio.Lock()


async def _candidate_for_symbol(symbol, bucket, *, deadline, direction="AUTO", minutes=5, verdict=None):
    """Only a closed broker-provided M1 candle can authorize a DEMO signal."""
    def reject(reason):
        if verdict: verdict(reason)
        return None
    if time.time()>=deadline:
        return reject('janela_expirada')
    # Historical signal warm-up requires >110 bars. Do not infer gaps.
    raw=await shared_candle_request(symbol,60,165,timeout=8)
    bars=strict_closed_bars(raw,60,now=time.time())
    if len(bars)<135 or bars[-1]["epoch"]+60!=bucket:
        return reject('velas_insuficientes')
    tail=bars[-135:]
    if any(tail[j]["epoch"]-tail[j-1]["epoch"]!=60 for j in range(1,len(tail))):
        return reject('velas_incompletas')
    rows=[[b["epoch"],b["open"],b["high"],b["low"],b["close"]]
          for b in tail]
    # AUTO must inspect both real PULSO directions. Explicit CALL/PUT
    # selections are unchanged; the PULSO's internal rules remain mandatory.
    variants=make_variants(rows)
    permitted=("CALL","PUT") if direction=="AUTO" else (direction,)
    side=None
    for attempt in permitted:
        signal=variants[("stable_volatility",attempt)][-1]
        if signal!=(1 if attempt=="CALL" else -1):
            continue
        if time.time()>=deadline:
            return reject('janela_expirada')
        side=attempt
        break
    if side is None:
        return reject('estrategia_sem_sinal')
    # Duration availability and payout are verified with actual DEMO proposals
    # AFTER a confirmed directional signal; do not make four extra contract
    # catalogue calls inside the narrow closed-candle scanner window.
    supported_expiries=[1,3,5,15] if minutes==0 else [minutes]
    return {
        "strategy":f"nexora_pulso_v2_stable_{side.lower()}_auto",
        "direction":side,"symbol":symbol,
        "minutes":supported_expiries[0],"expiry_options":supported_expiries,"bar_seconds":60,
        "last_bar_epoch":bars[-1]["epoch"],
        "reason":"PULSO V2: pressão eficiente + volatilidade estável; duração validada pelo executor",
        "score":1,
    }


async def confirmed_pulso_signal(allowed_symbols=None, direction="AUTO", minutes=5, scan_event=None):
    """Scan all Deriv-listed Volatility symbols (or single-symbol fallback).

    Return at most ONE live candidate per closed M1 bar. Fail closed on
    incomplete data, closed execution window, unsupported duration or errors.
    This is signal-only; order submission and risk gates belong to the selected
    DEMO or REAL executor.
    """
    now=time.time()
    if now%60>12:
        if scan_event: scan_event('window',None,len(allowed_symbols or []),'aguardando_fechamento_m1')
        return None
    async with _cache_lock:
        now=time.time()
        bucket=int(now//60)*60
        key=(direction,minutes,tuple(sorted(set(allowed_symbols))) if allowed_symbols is not None else None)
        cached=_cache.get(key)
        if cached and cached["last_bucket"]==bucket:
            if scan_event: scan_event('cached', None, len(allowed_symbols or []))
            return dict(cached["candidate"]) if cached["candidate"] else None
        # An unavailable broker response is not evidence of no signal.
        _cache[key]={"last_bucket":bucket,"candidate":None}
        incomplete_market_data=False
        # Bound memory across different user-selected portfolios.
        if len(_cache)>200:
            for k in list(_cache):
                if _cache[k]["last_bucket"]<bucket-120:
                    _cache.pop(k,None)
        deadline=bucket+8
        if allowed_symbols is not None:
            # Only symbols the user explicitly selected from the broker catalogue.
            symbols=sorted(set(allowed_symbols))
        elif SYMBOLS_MODE=="single":
            symbols=[SYMBOL]
        else:
            instruments=await asyncio.wait_for(catalogue("volatility"),timeout=5)
            symbols=sorted({i["symbol"] for i in instruments})
            if not symbols:
                _cache.pop(key,None)
                _log.warning("PULSO market catalogue empty; no DEMO signals")
                return None
        if allowed_symbols is not None:
            symbols=[symbol for symbol in symbols if symbol in set(allowed_symbols)]
        if not symbols:
            _cache.pop(key,None)
            return None
        if scan_event: scan_event('start', None, len(symbols))
        sem=asyncio.Semaphore(5)
        async def examine(symbol):
            nonlocal incomplete_market_data
            async with sem:
                if scan_event: scan_event('checking',symbol,len(symbols))
                reason='nao_classificado'
                def verdict(value):
                    nonlocal reason
                    reason=value
                try:
                    result=await _candidate_for_symbol(
                        symbol,bucket,deadline=deadline,direction=direction,
                        minutes=minutes,verdict=verdict)
                    if result: reason='sinal_confirmado'
                    return result
                except (MarketError,TimeoutError,OSError,TypeError,ValueError) as exc:
                    reason='dados_indisponiveis'
                    _log.info("PULSO M1 ignored symbol=%s error_type=%s",symbol,type(exc).__name__)
                    return None
                finally:
                    if reason in ('dados_indisponiveis','velas_insuficientes',
                                  'velas_incompletas','janela_expirada'):
                        incomplete_market_data=True
                    if scan_event: scan_event('checked',symbol,len(symbols),reason)
        # Execute bounded asynchronous calls, never beyond candle entry window.
        remaining=deadline-time.time()
        if remaining<=1:
            _cache.pop(key,None)
            return None
        tasks=[asyncio.create_task(examine(symbol)) for symbol in symbols]
        try:
            # Preserve completed, broker-confirmed signals when another symbol
            # has a slow response. No late tasks may authorize an entry.
            done,pending=await asyncio.wait(
                tasks,timeout=max(.5,remaining-0.4))
            if pending:
                incomplete_market_data=True
                _log.warning("PULSO M1 partial scan: completed=%s pending=%s",
                             len(done),len(pending))
                if scan_event: scan_event('timeout',None,len(symbols))
            candidates=[]
            for task in done:
                if task.cancelled() or task.exception() is not None:
                    incomplete_market_data=True
                    continue
                candidate=task.result()
                if candidate is not None:
                    candidates.append(candidate)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
        if candidates and time.time()<deadline:
            # Rotate fair ordering by minute; never turn historical results
            # from 1HZ100V into claims about other assets.
            candidates.sort(key=lambda x:x["symbol"])
            chosen=candidates[(bucket//60)%len(candidates)]
            _cache[key]["candidate"]=chosen
            if scan_event: scan_event('signal',chosen["symbol"],len(symbols))
            _log.info("PULSO ready: eligible_symbols=%s signals=%s chosen=%s",
                      len(symbols),len(candidates),chosen["symbol"])
            return dict(chosen)
        if incomplete_market_data:
            # An API outage/late M1 candle can recover before the entry window
            # closes. Never freeze that failure into a false no-signal verdict.
            _cache.pop(key,None)
        return None

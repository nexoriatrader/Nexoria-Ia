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


async def _candidate_for_symbol(symbol, bucket, *, deadline, direction="AUTO", minutes=5, indicators=(), verdict=None):
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
    # selections are unchanged; all technical filters remain mandatory.
    variants=make_variants(rows)
    permitted=("CALL","PUT") if direction=="AUTO" else (direction,)
    side=None
    direction_detected=False
    for attempt in permitted:
        signal=variants[("stable_volatility",attempt)][-1]
        if signal!=(1 if attempt=="CALL" else -1):
            continue
        direction_detected=True
        if time.time()>=deadline:
            return reject('janela_expirada')
        if indicators:
            from .automation_signals import _indicators_agree
            if not _indicators_agree(tail,attempt,indicators,"pulso_v2"):
                continue
        side=attempt
        break
    if side is None:
        return reject('indicadores_nao_confirmados' if direction_detected else 'estrategia_sem_sinal')
    # Duration availability and payout are verified with actual DEMO proposals
    # AFTER a confirmed directional signal; do not make four extra contract
    # catalogue calls inside the narrow closed-candle scanner window.
    supported_expiries=[1,3,5,15] if minutes==0 else [minutes]
    return {
        "strategy":f"nexora_pulso_v2_stable_{side.lower()}_auto",
        "direction":side,"symbol":symbol,
        "minutes":supported_expiries[0],"expiry_options":supported_expiries,"bar_seconds":60,
        "last_bar_epoch":bars[-1]["epoch"],
        "reason":"PULSO V2: pressão eficiente + volatilidade estável; expiração escolhida pela proposta DEMO",
        "score":1,
    }


async def confirmed_pulso_signal(allowed_symbols=None, direction="AUTO", minutes=5, indicators=(), scan_event=None):
    """Scan all Deriv-listed Volatility symbols (or single-symbol fallback).

    Return at most ONE live candidate per closed M1 bar. Fail closed on
    incomplete data, closed execution window, unsupported duration or errors.
    Existing DEMO reconciliation and official risk gates remain in main.py.
    """
    now=time.time()
    if now%60>12:
        if scan_event: scan_event('window',None,len(allowed_symbols or []),'aguardando_fechamento_m1')
        return None
    async with _cache_lock:
        now=time.time()
        bucket=int(now//60)*60
        key=(direction,minutes,tuple(sorted(set(indicators))),tuple(sorted(set(allowed_symbols))) if allowed_symbols is not None else None)
        cached=_cache.get(key)
        if cached and cached["last_bucket"]==bucket:
            if scan_event: scan_event('cached', None, len(allowed_symbols or []))
            return dict(cached["candidate"]) if cached["candidate"] else None
        _cache[key]={"last_bucket":bucket,"candidate":None}
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
                _log.warning("PULSO market catalogue empty; no DEMO signals")
                return None
        if allowed_symbols is not None:
            symbols=[symbol for symbol in symbols if symbol in set(allowed_symbols)]
        if not symbols:
            return None
        if scan_event: scan_event('start', None, len(symbols))
        sem=asyncio.Semaphore(5)
        async def examine(symbol):
            async with sem:
                if scan_event: scan_event('checking',symbol,len(symbols))
                reason='nao_classificado'
                def verdict(value):
                    nonlocal reason
                    reason=value
                try:
                    result=await _candidate_for_symbol(
                        symbol,bucket,deadline=deadline,direction=direction,
                        minutes=minutes,indicators=indicators,verdict=verdict)
                    if result: reason='sinal_confirmado'
                    return result
                except (MarketError,TimeoutError,OSError,TypeError,ValueError) as exc:
                    reason='dados_indisponiveis'
                    _log.info("PULSO M1 ignored symbol=%s error_type=%s",symbol,type(exc).__name__)
                    return None
                finally:
                    if scan_event: scan_event('checked',symbol,len(symbols),reason)
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
            if scan_event: scan_event('timeout',None,len(symbols))
            return None
        candidates=[item for item in results if item]
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
        return None

"""Fixed-rule closed-candle signal scanner for Deriv Options automation.

Strategies use broker-provided completed M1 candles, selected symbols,
requested expiry and direction. Each strategy owns its entry and confirmation
rules; signals do not issue a broker order.
"""
from __future__ import annotations

import asyncio
import json
import time

from .market import MarketError, public_request, shared_candle_request
from .trade_selector_v2 import strict_closed_bars, supported
from .pulso_live import confirmed_pulso_signal


def _ema(values, period):
    if not values or len(values) < period:
        return []
    weight = 2.0 / (period + 1)
    first = sum(values[:period]) / period
    out = [None] * (period - 1) + [first]
    for value in values[period:]:
        out.append(out[-1] + weight * (value - out[-1]))
    return out


def _rsi(values, period=14):
    if len(values) < period + 2:
        return None
    changes = [b-a for a, b in zip(values, values[1:])]
    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]
    avg_g = sum(gains[:period])/period
    avg_l = sum(losses[:period])/period
    series = [None]*period
    for i in range(period, len(changes)):
        avg_g = (avg_g*(period-1) + gains[i])/period
        avg_l = (avg_l*(period-1) + losses[i])/period
        series.append(100 if avg_l==0 else 100 - 100/(1+avg_g/avg_l))
    return series[-2],series[-1]


def _direction_for(rows, strategy):
    closes = [r['close'] for r in rows]
    opens = [r['open'] for r in rows]
    if strategy == 'trend':
        fast,slow = _ema(closes,20),_ema(closes,50)
        if fast[-1] is None or slow[-1] is None:
            return None
        if (fast[-1]>slow[-1] and closes[-2]<=fast[-2] and
                closes[-1]>fast[-1] and closes[-1]>opens[-1]):
            return 'CALL'
        if (fast[-1]<slow[-1] and closes[-2]>=fast[-2] and
                closes[-1]<fast[-1] and closes[-1]<opens[-1]):
            return 'PUT'
    elif strategy == 'rsi':
        levels=_rsi(closes)
        if levels is None:
            return None
        before,after=levels
        if before<=30 and after>30 and closes[-1]>opens[-1]:
            return 'CALL'
        if before>=70 and after<70 and closes[-1]<opens[-1]:
            return 'PUT'
    return None


_scan_cache = {}
_scan_lock = asyncio.Lock()


async def confirmed_automation_signal(profile, scan_event=None, excluded_symbols=()):
    """Return one signal on a fresh closed M1 candle, or None."""
    excluded=set(excluded_symbols)
    symbols=[symbol for symbol in json.loads(profile.symbols_json)
             if symbol not in excluded]
    if not symbols:
        return None
    direction=profile.direction
    if profile.strategy=='pulso_v2':
        return await confirmed_pulso_signal(symbols,direction=direction,minutes=profile.expiry_minutes,scan_event=scan_event)

    now=time.time()
    if now%60>12:
        if scan_event: scan_event('window',None,len(symbols),'aguardando_fechamento_m1')
        return None
    bucket=int(now//60)*60
    key=(profile.strategy,profile.expiry_minutes,direction,tuple(symbols))
    async with _scan_lock:
        cached=_scan_cache.get(key)
        if cached and cached[0]==bucket:
            if scan_event: scan_event('cached',None,len(symbols))
            return dict(cached[1]) if cached[1] else None
        _scan_cache[key]=(bucket,None)
        if len(_scan_cache)>300:
            for k,v in list(_scan_cache.items()):
                if v[0]<bucket-120:
                    _scan_cache.pop(k,None)

        # Rotate the symbol batch to cover even very large portfolios.
        unique=sorted(set(symbols))
        batch_size=min(8,len(unique))
        first=(bucket//60*batch_size)%len(unique)
        batch=[unique[(first+i)%len(unique)] for i in range(batch_size)]
        if scan_event: scan_event('start',None,len(batch))
        deadline=bucket+8
        sem=asyncio.Semaphore(4)

        async def check(symbol):
            async with sem:
                if scan_event: scan_event('checking',symbol,len(batch))
                reason='nao_classificado'
                try:
                    if time.time()>=deadline:
                        reason='janela_expirada'
                        return None
                    raw=await shared_candle_request(symbol,60,165,timeout=7)
                    closed=strict_closed_bars(raw,60,now=time.time())
                    if len(closed)<85 or closed[-1]['epoch']+60!=bucket:
                        reason='velas_insuficientes'
                        return None
                    rows=closed[-85:]
                    if any(rows[i]['epoch']-rows[i-1]['epoch']!=60
                           for i in range(1,len(rows))):
                        reason='velas_incompletas'
                        return None
                    side=_direction_for(rows,profile.strategy)
                    if not side or (direction!='AUTO' and side!=direction):
                        reason='estrategia_sem_sinal'
                        return None
                    if time.time()>=deadline:
                        reason='janela_expirada'
                        return None
                    # Broker proposal checks determine supported expirations,
                    # rather than spending the limited signal window querying
                    # contracts_for four times for every candidate.
                    expiry_options=[1,3,5,15] if profile.expiry_minutes==0 else [profile.expiry_minutes]
                    reason='sinal_confirmado'
                    return {
                        'strategy':'nexora_'+profile.strategy+'_m1_auto',
                        'direction':side,'symbol':symbol,
                        'minutes':expiry_options[0],'expiry_options':expiry_options,'bar_seconds':60,
                        'last_bar_epoch':rows[-1]['epoch'],
                        'reason':profile.strategy+' · fechamento M1',
                        'score':1
                    }
                except (MarketError,TimeoutError,OSError,ValueError,TypeError,IndexError):
                    reason='dados_indisponiveis'
                    return None
                finally:
                    if scan_event: scan_event('checked',symbol,len(batch),reason)

        tasks=[asyncio.create_task(check(x)) for x in batch]
        try:
            done,pending=await asyncio.wait(tasks,timeout=max(0.1,deadline-time.time()-0.3))
            for task in pending:
                task.cancel()
            candidates=[task.result() for task in done if not task.cancelled()
                        and task.exception() is None and task.result() is not None]
            if candidates and time.time()<deadline:
                candidates.sort(key=lambda item:item['symbol'])
                result=candidates[0]
                _scan_cache[key]=(bucket,result)
                if scan_event: scan_event('signal',result['symbol'],len(batch))
                return dict(result)
            return None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

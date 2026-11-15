"""Configurable closed-candle signal scanner for Deriv Options automation.

Strategies use only broker-provided completed M1 candles, selected symbols,
requested expiry, direction and optional indicator confirmations. Signals do
not issue a broker order; the order executor remains a separate concern.
"""
from __future__ import annotations

import asyncio
import json
import math
import time

from .market import MarketError, public_request
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


def _indicators_agree(rows, direction, indicators, strategy):
    if not indicators:
        return True
    closes=[r['close'] for r in rows]
    opens=[r['open'] for r in rows]
    high=[r['high'] for r in rows]
    low=[r['low'] for r in rows]
    positive=direction=='CALL'
    if 'EMA' in indicators:
        a,b=_ema(closes,20),_ema(closes,50)
        if (a[-1] > b[-1]) != positive:
            return False
    if 'SMA' in indicators:
        a=sum(closes[-20:])/20;b=sum(closes[-50:])/50
        if (a>b)!=positive:
            return False
    if 'RSI' in indicators and strategy!='rsi':
        values=_rsi(closes)
        if values is None or ((values[-1]>50)!=positive):
            return False
    if 'MACD' in indicators:
        a,b=_ema(closes,12),_ema(closes,26)
        if (a[-1]>b[-1]) != positive:
            return False
    if 'BOLLINGER' in indicators:
        middle=sum(closes[-20:])/20
        sigma=math.sqrt(sum((x-middle)**2 for x in closes[-20:])/20)
        if sigma==0 or abs(closes[-1]-middle)>2*sigma:
            return False
    if 'ATR' in indicators:
        ranges=[max(h-l,abs(h-p),abs(l-p)) for h,l,p
                in zip(high[1:],low[1:],closes[:-1])]
        if sum(ranges[-14:])/14 < .7*sum(ranges[-30:])/30:
            return False
    if 'ADX' in indicators:
        ranges=[max(h-l,abs(h-p),abs(l-p)) for h,l,p
                in zip(high[1:],low[1:],closes[:-1])]
        atr=sum(ranges[-14:])/14
        directional=abs(closes[-1]-closes[-15])/14
        if atr<=0 or directional/atr < .15:
            return False
    if 'STOCHASTIC' in indicators:
        hi=max(high[-14:]);lo=min(low[-14:])
        if hi==lo or ((closes[-1]-lo)/(hi-lo)>0.5)!=positive:
            return False
    if 'SUPPORT_RESISTANCE' in indicators:
        if positive and closes[-1]<=max(high[-21:-1]):
            return False
        if not positive and closes[-1]>=min(low[-21:-1]):
            return False
    if 'CANDLE_PATTERNS' in indicators:
        a,b=rows[-2],rows[-1]
        if positive and not (a['close']<a['open'] and b['close']>b['open']
                             and b['close']>=a['open'] and b['open']<=a['close']):
            return False
        if not positive and not (a['close']>a['open'] and b['close']<b['open']
                                 and b['close']<=a['open'] and b['open']>=a['close']):
            return False
    return True


_scan_cache = {}
_scan_lock = asyncio.Lock()


async def confirmed_automation_signal(profile):
    """Return one signal on a fresh closed M1 candle, or None."""
    symbols=json.loads(profile.symbols_json)
    if not symbols:
        return None
    indicators=json.loads(profile.indicators_json or '[]')
    direction=profile.direction
    if profile.strategy=='pulso_v2':
        return await confirmed_pulso_signal(symbols,direction=direction,minutes=profile.expiry_minutes,indicators=indicators)

    now=time.time()
    if now%60>12:
        return None
    bucket=int(now//60)*60
    key=(profile.strategy,profile.expiry_minutes,direction,tuple(symbols),tuple(indicators))
    async with _scan_lock:
        cached=_scan_cache.get(key)
        if cached and cached[0]==bucket:
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
        deadline=bucket+12
        sem=asyncio.Semaphore(4)

        async def check(symbol):
            async with sem:
                if time.time()>=deadline:
                    return None
                try:
                    if not await supported(symbol,minutes=profile.expiry_minutes):
                        return None
                    raw=await public_request({
                        'ticks_history':symbol,'style':'candles','granularity':60,
                        'count':115,'end':'latest'
                    },expected='candles',timeout=7)
                    closed=strict_closed_bars(raw,60,now=time.time())
                    if len(closed)<85 or closed[-1]['epoch']+60!=bucket:
                        return None
                    rows=closed[-85:]
                    if any(rows[i]['epoch']-rows[i-1]['epoch']!=60
                           for i in range(1,len(rows))):
                        return None
                    side=_direction_for(rows,profile.strategy)
                    if not side or (direction!='AUTO' and side!=direction):
                        return None
                    if not _indicators_agree(rows,side,indicators,profile.strategy):
                        return None
                    if time.time()>=deadline:
                        return None
                    return {
                        'strategy':'nexora_'+profile.strategy+'_m1',
                        'direction':side,'symbol':symbol,
                        'minutes':profile.expiry_minutes,'bar_seconds':60,
                        'last_bar_epoch':rows[-1]['epoch'],
                        'reason':profile.strategy+' · fechamento M1 · '+','.join(indicators),
                        'score':1
                    }
                except (MarketError,TimeoutError,OSError,ValueError,TypeError,IndexError):
                    return None

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
                return dict(result)
            return None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

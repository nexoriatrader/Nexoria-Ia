"""Fixed-rule closed-candle signal scanner for Deriv Options automation.

Strategies use broker-provided completed candles, selected symbols, requested
expiry and direction. Each strategy owns its entry and confirmation rules;
signals do not issue a broker order.
"""
from __future__ import annotations

import asyncio
import json
import time
from statistics import mean

from .market import MarketError, public_request, shared_candle_request
from .trade_selector_v2 import strict_closed_bars, supported
from .pulso_live import confirmed_pulso_signal
from .strategy_registry import FOREX_DEMO_STRATEGIES


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


def _continuous_tail(rows, seconds, count):
    return (len(rows) >= count and
            all(rows[i]['epoch'] - rows[i-1]['epoch'] == seconds
                for i in range(len(rows)-count+1, len(rows))))


def _forex_h1_trend(h1_rows):
    """H1 direction: close and rising/falling EMA50, using closed bars only."""
    if len(h1_rows) < 54 or not _continuous_tail(h1_rows, 3600, 54):
        return None
    closes = [row['close'] for row in h1_rows]
    ema = _ema(closes, 50)
    if len(ema) < 4 or any(value is None for value in ema[-4:]):
        return None
    if closes[-1] > ema[-1] and ema[-1] > ema[-4]:
        return 'CALL'
    if closes[-1] < ema[-1] and ema[-1] < ema[-4]:
        return 'PUT'
    return None


def forex_multitimeframe_signal(strategy, h1_rows, m15_rows, *, now):
    """Return an H1-trend/M15-entry Forex candidate from completed candles.

    This is an experimental signal rule, not an estimate of profitability.
    """
    if strategy not in FOREX_DEMO_STRATEGIES:
        return None
    m15_required = 55 if strategy == 'fx_pullback_m15' else 21
    if len(h1_rows) < 54 or len(m15_rows) < m15_required:
        return None
    if not _continuous_tail(h1_rows, 3600, 7):
        return None
    if not _continuous_tail(m15_rows, 900, m15_required):
        return None

    signal_bar = m15_rows[-1]
    signal_close = signal_bar['epoch'] + 900
    age = now - signal_close
    if not 0 <= age <= 20:
        return None
    h1_close = h1_rows[-1]['epoch'] + 3600
    if h1_close > signal_close or signal_close - h1_close > 3600:
        return None

    direction = _forex_h1_trend(h1_rows)
    if direction is None:
        return None
    if strategy == 'fx_pullback_m15':
        closes = [row['close'] for row in m15_rows]
        ema = _ema(closes, 50)
        if len(ema) < 5 or any(value is None for value in ema[-5:]):
            return None
        ranges = [row['high'] - row['low'] for row in m15_rows[-15:-1]]
        average_range = mean(ranges) if ranges else 0
        if average_range <= 0:
            return None
        confirm = m15_rows[-1]
        previous = m15_rows[-2]
        confirm_range = confirm['high'] - confirm['low']
        if not 0.35 * average_range <= confirm_range <= 2.5 * average_range:
            return None
        if direction == 'CALL':
            confirmed = (confirm['close'] > confirm['open']
                         and confirm['close'] > previous['high']
                         and confirm['close'] > ema[-1])
            touched = any(
                abs(m15_rows[-offset]['low'] - ema[-offset]) <= 0.35 * average_range
                and m15_rows[-offset]['close'] >= ema[-offset] - 0.5 * average_range
                for offset in range(2, 6))
        else:
            confirmed = (confirm['close'] < confirm['open']
                         and confirm['close'] < previous['low']
                         and confirm['close'] < ema[-1])
            touched = any(
                abs(m15_rows[-offset]['high'] - ema[-offset]) <= 0.35 * average_range
                and m15_rows[-offset]['close'] <= ema[-offset] + 0.5 * average_range
                for offset in range(2, 6))
        if not (confirmed and touched):
            return None
        reason = ('Forex · tendência H1 alinhada; pullback e confirmação M15 '
                  'em candles fechados; expiração fixa M15')
    else:
        prior = m15_rows[-21:-1]
        if len(prior) != 20:
            return None
        confirm = m15_rows[-1]
        upper = max(row['high'] for row in prior)
        lower = min(row['low'] for row in prior)
        if direction == 'CALL':
            confirmed = confirm['close'] > confirm['open'] and confirm['close'] > upper
        else:
            confirmed = confirm['close'] < confirm['open'] and confirm['close'] < lower
        if not confirmed:
            return None
        reason = ('Forex · tendência H1 alinhada; fechamento M15 rompeu o canal '
                  'anterior de 20 candles; expiração fixa M15')

    return {
        'strategy': 'nexora_' + strategy,
        'direction': direction,
        'symbol': None,
        'minutes': 15,
        'expiry_options': [15],
        'bar_seconds': 900,
        'last_bar_epoch': signal_bar['epoch'],
        'deadline_epoch': signal_close + 25,
        'reason': reason,
    }


_scan_cache = {}
_scan_lock = asyncio.Lock()


async def _confirmed_forex_signal(profile, scan_event=None, excluded_symbols=()):
    """Scan only Forex DEMO profiles; this routine never submits an order."""
    if (profile.market != 'forex' or profile.account_mode != 'demo'
        or profile.strategy not in FOREX_DEMO_STRATEGIES):
        return None
    excluded = set(excluded_symbols)
    symbols = [symbol for symbol in json.loads(profile.symbols_json)
               if symbol not in excluded]
    if not symbols:
        return None

    now = time.time()
    slot = int(now // 900) * 900
    if not 0 <= now - slot <= 20:
        if scan_event:
            scan_event('window', None, len(symbols), 'aguardando_fechamento_m15')
        return None
    key = (profile.strategy, 15, profile.direction, tuple(symbols))
    async with _scan_lock:
        cached = _scan_cache.get(key)
        if cached and cached[0] == slot:
            if scan_event:
                scan_event('cached', None, len(symbols))
            return dict(cached[1]) if cached[1] else None
        _scan_cache[key] = (slot, None)
        if len(_scan_cache) > 300:
            for old_key, value in list(_scan_cache.items()):
                if value[0] < slot - 1800:
                    _scan_cache.pop(old_key, None)

        unique = sorted(set(symbols))
        batch_size = min(5, len(unique))
        batch = unique[:batch_size]
        if scan_event:
            scan_event('start', None, len(batch))
        semaphore = asyncio.Semaphore(4)

        async def check(symbol):
            reason = 'sinal_nao_confirmado'
            async with semaphore:
                if scan_event:
                    scan_event('checking', symbol, len(batch))
                try:
                    if time.time() > slot + 20:
                        reason = 'janela_m15_expirada'
                        return None
                    if not await supported(symbol, minutes=15):
                        reason = 'call_put_15m_indisponivel'
                        return None
                    raw_h1, raw_m15 = await asyncio.gather(
                        shared_candle_request(symbol, 3600, 65, timeout=8),
                        shared_candle_request(symbol, 900, 75, timeout=8),
                    )
                    checked_at = time.time()
                    h1 = strict_closed_bars(raw_h1, 3600, now=checked_at)
                    m15 = strict_closed_bars(raw_m15, 900, now=checked_at)
                    candidate = forex_multitimeframe_signal(
                        profile.strategy, h1, m15, now=checked_at)
                    if candidate is None:
                        reason = 'criterios_mtf_nao_confirmados'
                        return None
                    if profile.direction != 'AUTO' and candidate['direction'] != profile.direction:
                        reason = 'direcao_nao_permitida'
                        return None
                    candidate['symbol'] = symbol
                    reason = 'sinal_confirmado'
                    return candidate
                except (MarketError, TimeoutError, OSError, ValueError,
                        TypeError, IndexError, KeyError):
                    reason = 'dados_indisponiveis'
                    return None
                finally:
                    if scan_event:
                        scan_event('checked', symbol, len(batch), reason)

        tasks = [asyncio.create_task(check(symbol)) for symbol in batch]
        try:
            done, pending = await asyncio.wait(tasks, timeout=7)
            for task in pending:
                task.cancel()
            candidates = [task.result() for task in done
                          if not task.cancelled() and task.exception() is None
                          and task.result() is not None]
            if candidates:
                candidates.sort(key=lambda item: item['symbol'])
                result = candidates[0]
                _scan_cache[key] = (slot, result)
                if scan_event:
                    scan_event('signal', result['symbol'], len(batch))
                return dict(result)
            return None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()


async def confirmed_automation_signal(profile, scan_event=None, excluded_symbols=()):
    """Return one strategy-owned signal from fresh, completed candles, or None."""
    if profile.strategy in FOREX_DEMO_STRATEGIES:
        return await _confirmed_forex_signal(profile, scan_event, excluded_symbols)
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

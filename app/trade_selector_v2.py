"""Experimental, DEMO-only candidate discovery from official Deriv public candles.

No historical-directional strategy is claimed profitable; broker DEMO settlements
provide the actual evaluation. Never invent symbols or buy from this module.
"""
from __future__ import annotations
import asyncio
import re
import time
from .market import MarketError, discover_symbols, discover_contracts
from .strategy_lab import fetch_candles, direction_at

FX_PAIRS=('EURJPY','EURUSD','GBPUSD','USDJPY','GBPJPY')
MAX_SYMBOLS=8
EXPIRY={'range_break':3,'forex':3,'step':5,'volatility':5}
_catalog_cache=()
_catalog_at=0.0
_contract_cache={}


def classify(item):
    symbol=item.get('symbol','')
    label=item.get('display_name','')
    if not isinstance(symbol,str) or not isinstance(label,str):
        return None
    if not re.fullmatch(r'[A-Za-z0-9]{2,30}',symbol):
        return None
    normalized=re.sub('[^A-Z]','',label.upper())
    code=symbol.upper()
    for pair in FX_PAIRS:
        if normalized==pair or (pair in normalized and ('FOREX' in normalized or 'CURRENCY' in normalized)) or code in (pair,'FRX'+pair):
            return 'forex',pair
    lower=label.lower()
    if 'range break' in lower:
        return 'range_break','range_break'
    if 'step' in lower and 'skew' not in lower and 'multi' not in lower:
        return 'step','step'
    vol=re.search(r'volatility\s*(10|25|50|75|100)\b',lower)
    if vol:
        return 'volatility','vol'+vol.group(1)
    return None


def choose_tradeable(symbols):
    """Only items returned by the Deriv catalogue, never assumed identifiers."""
    picked={}
    for item in symbols:
        if not isinstance(item,dict):
            continue
        kind=classify(item)
        if not kind:
            continue
        family,group=kind
        if group not in picked:
            picked[group]={**item,'family':family,'group':group}
    order=list(FX_PAIRS)+['range_break','step','vol10','vol25','vol50','vol75','vol100']
    return [picked[key] for key in order if key in picked][:MAX_SYMBOLS]


async def catalogue():
    global _catalog_at,_catalog_cache
    now=time.monotonic()
    if _catalog_cache and now-_catalog_at<300:
        return list(_catalog_cache)
    found=choose_tradeable(await discover_symbols())
    _catalog_cache=tuple(found)
    _catalog_at=now
    return found


async def supported(symbol):
    now=time.monotonic()
    cached=_contract_cache.get(symbol)
    if cached and now-cached[0]<300:
        return cached[1]
    types=await discover_contracts(symbol)
    ok={'CALL','PUT'}.issubset(set(types))
    _contract_cache[symbol]=(now,ok)
    return ok


def signal_from_closed_candles(item,candles,now=None):
    """Return only a newly closed M1 signal, with gap and age checks."""
    now=time.time() if now is None else now
    if len(candles)<60:
        return None
    tail=candles[-55:]
    if any(b['epoch']-a['epoch']!=60 for a,b in zip(tail,tail[1:])):
        return None
    last=tail[-1]
    age=now-(last['epoch']+60)
    if age<0 or age>50:
        return None
    family=item['family']
    strategy='range_channel' if family in ('range_break','forex') else 'step_ma'
    direction=direction_at(tail,len(tail)-1,strategy)
    if direction not in ('CALL','PUT'):
        return None
    return {'symbol':item['symbol'],'display_name':item['display_name'],
            'strategy':('rompimento_m1' if strategy=='range_channel' else 'tendencia_m1'),
            'direction':direction,'minutes':EXPIRY[family],
            'family':family,'last_bar_epoch':last['epoch'],
            'reason':('Rompimento do canal de 20 velas M1 fechadas' if strategy=='range_channel'
                      else 'Cruzamento de médias 8/21 em velas M1 fechadas')}


async def select_demo_signal(*, exclude_symbols=()):
    """Returns one experimental candidate, never an execution authorization."""
    items=await catalogue()
    if not items:
        return None
    sem=asyncio.Semaphore(3)
    async def examine(item):
        async with sem:
            try:
                if not await supported(item['symbol']):
                    return None
                bars=await fetch_candles(item['symbol'],count=120)
                return signal_from_closed_candles(item,bars)
            except (MarketError,ValueError,TypeError,TimeoutError):
                return None
    results=await asyncio.gather(*(examine(i) for i in items))
    # Rompimento preferred over crossover. No promise of profitable execution.
    excluded=set(exclude_symbols)
    signals=[x for x in results if x and x['symbol'] not in excluded]
    if not signals:
        return None
    signals.sort(key=lambda x:(0 if x['strategy']=='rompimento_m1' else 1,
                               x['symbol']))
    return signals[0]

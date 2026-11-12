"""NEXORA Strategy Lab: read-only out-of-sample research for Deriv derived indices.

Important: directional candle outcomes are NOT Deriv settled option contracts;
profit, payout and execution quality cannot be inferred from the hit rate.
The live AutoPilot is deliberately unchanged by these experiments.
"""
from __future__ import annotations
from datetime import datetime, timezone
from time import time
import re
from math import isfinite
from statistics import mean
from .market import MarketError, public_request, discover_symbols, discover_contracts

MAX_BARS=1000
EXPIRIES=(1,3,5)
MIN_SIGNALS=15


def family_of(display_name: str) -> str | None:
    name=display_name.lower().replace('-', ' ')
    if 'range break' in name:
        return 'range_break'
    # "step" alone; not Skew Step / Multi Step families.
    if 'step' in name and 'skew' not in name and 'multi' not in name:
        return 'step'
    return None


async def candidates() -> list[dict]:
    symbols=await discover_symbols()
    seen=set()
    found=[]
    for item in symbols:
        family=family_of(item['display_name'])
        if family and family not in seen:
            found.append({**item,'family':family})
            seen.add(family)
    return found


def parse_candles(payload: dict) -> list[dict]:
    raw=payload.get('candles')
    if not isinstance(raw,list):
        raise MarketError('A Deriv não forneceu velas históricas para este ativo')
    candles=[]
    for candle in raw:
        try:
            epoch=int(candle['epoch'])
            close=float(candle['close'])
            high=float(candle['high'])
            low=float(candle['low'])
        except (KeyError,TypeError,ValueError,OverflowError):
            raise MarketError('Candle histórico inválido') from None
        if not all(isfinite(x) and x>0 for x in (close,high,low)):
            raise MarketError('Cotação histórica não finita')
        if low>high or not low<=close<=high:
            raise MarketError('Inconsistência de OHLC na Deriv')
        candles.append({'epoch':epoch,'close':close,'high':high,'low':low})
    candles.sort(key=lambda x:x['epoch'])
    if len({x['epoch'] for x in candles})!=len(candles):
        raise MarketError('Dados históricos com timestamps duplicados')
    # Exclude the still-forming current minute from all calculations.
    return [bar for bar in candles if bar['epoch']+60<=int(time())]


async def fetch_candles(symbol: str, *, count: int=MAX_BARS) -> list[dict]:
    if not isinstance(symbol,str) or not re.fullmatch(r'[A-Za-z0-9_]{2,30}',symbol):
        raise MarketError('Símbolo inválido')
    payload=await public_request({
        'ticks_history':symbol,'style':'candles','granularity':60,
        'count':min(count,MAX_BARS),'end':'latest'
    },expected='candles',timeout=18)
    return parse_candles(payload)


def direction_at(data: list[dict], i: int, strategy: str) -> str|None:
    """Signal from complete bars up to i; never reads the future bar."""
    if i<26:
        return None
    prices=[bar['close'] for bar in data]
    if strategy=='step_ma':
        fast=mean(prices[i-7:i+1])
        slow=mean(prices[i-20:i+1])
        prevfast=mean(prices[i-8:i])
        prevslow=mean(prices[i-21:i])
        # Crossovers only: avoid one signal on every bar of the same trend.
        if fast>slow and prevfast<=prevslow:
            return 'CALL'
        if fast<slow and prevfast>=prevslow:
            return 'PUT'
    elif strategy=='range_channel':
        prior=data[i-20:i]
        upper=max(x['high'] for x in prior)
        lower=min(x['low'] for x in prior)
        curr=data[i]['close']
        prev=data[i-1]['close']
        if curr>upper and prev<=upper:
            return 'CALL'
        if curr<lower and prev>=lower:
            return 'PUT'
    return None


def score(data: list[dict], strategy: str, minutes: int, *,
          start: int, end: int) -> dict:
    if minutes not in EXPIRIES:
        raise ValueError('Expiração inválida')
    wins=losses=draws=0
    positions=[]
    i=max(start,26)
    # Evaluate only signals whose ENTIRE duration stays inside the partition.
    while i+minutes<=end:
        # A 5-minute expiry means 5 contiguous minute intervals, not 5
        # arbitrarily distant candles across a feed/market gap.
        segment=data[i-26:i+minutes+1]
        if any(b['epoch']-a['epoch']!=60 for a,b in zip(segment,segment[1:])):
            i+=1
            continue
        direction=direction_at(data,i,strategy)
        if direction:
            # The next completed minute is the first evaluation point.
            # Entry at candle i close is an approximation; live execution differs.
            entry=data[i]['close']
            exit_price=data[i+minutes]['close']
            delta=exit_price-entry
            outcome=('DRAW' if delta==0 else
                     'WIN' if (delta>0)==(direction=='CALL') else 'LOSS')
            wins+=int(outcome=='WIN')
            losses+=int(outcome=='LOSS')
            draws+=int(outcome=='DRAW')
            positions.append({'at':data[i]['epoch'],'direction':direction,'outcome':outcome})
            i+=minutes  # do not overlap hypothetical contracts
        else:
            i+=1
    n=wins+losses+draws
    return {'signals':n,'wins':wins,'losses':losses,'draws':draws,
            'hit_rate':round(wins/n,4) if n else None,
            'first_signal':positions[0]['at'] if n else None,
            'last_signal':positions[-1]['at'] if n else None}


def evaluate(data:list[dict], family:str, symbol:str, display_name:str)->dict:
    if family not in ('range_break','step'):
        raise ValueError('Família não suportada')
    if len(data)<150:
        raise MarketError('Amostra insuficiente: mínimo de 150 velas M1')
    # Time-ordered split; never tune on the validation slice.
    split=int(len(data)*0.7)
    strategy='range_channel' if family=='range_break' else 'step_ma'
    results=[]
    for minutes in EXPIRIES:
        train=score(data,strategy,minutes,start=26,end=split-1)
        test=score(data,strategy,minutes,start=split,end=len(data)-1)
        results.append({'duration_minutes':minutes,'train':train,'validation':test,
                        'enough_validation_samples':test['signals']>=MIN_SIGNALS})
    return {
        'symbol':symbol,'display_name':display_name,'family':family,
        'strategy':strategy,'candles':len(data),'timeframe':'M1',
        'period_from':datetime.fromtimestamp(data[0]['epoch'],timezone.utc).isoformat(),
        'period_to':datetime.fromtimestamp(data[-1]['epoch'],timezone.utc).isoformat(),
        'split':'70% treino / 30% validação cronológica',
        'results':results,'contract_compatibility':'NOT_VERIFIED',
        'payout_adjusted':False,'orders_sent':0,
        'warning':'Taxa direcional em velas históricas, não resultados de opções Deriv. '
                  'Sem cotações de payout por época, latência ou preço executado; '
                  'não usar para liberar compras automáticas.'
    }


async def run_lab() -> dict:
    choices=await candidates()
    if not choices:
        return {'state':'NO_COMPATIBLE_SYMBOLS','items':[],
                'reason':'Não foram encontrados símbolos públicos Range Break ou Step no catálogo atual'}
    items=[]
    errors=[]
    for item in choices:
        try:
            supported=await discover_contracts(item['symbol'])
            if not {'CALL','PUT'}.issubset(set(supported)):
                errors.append({'symbol':item['symbol'],'reason':'CALL/PUT indisponíveis para esse ativo'})
                continue
            candles=await fetch_candles(item['symbol'])
            report=evaluate(candles,item['family'],item['symbol'],item['display_name'])
            report['contract_compatibility']='CALL_PUT_LISTED_DURATION_UNVERIFIED'
            items.append(report)
        except MarketError as e:
            errors.append({'symbol':item['symbol'],'reason':str(e)})
    return {'state':'RESEARCH_ONLY','items':items,'errors':errors,
            'strategy_selection':'STATIC_RESEARCH_NOT_CONNECTED_TO_AUTOPILOT',
            'orders_sent':0,'real_orders_enabled':False,
            'warning':'Estudo direcional preliminar. Nenhuma estratégia foi aprovada para negociação.'}

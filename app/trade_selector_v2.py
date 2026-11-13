"""Experimental, DEMO-only candidate discovery from official Deriv public candles.

No historical-directional strategy is claimed profitable; broker DEMO settlements
provide the actual evaluation. Never invent symbols or buy from this module.
"""
from __future__ import annotations
import asyncio
import re
import time
import logging
from datetime import datetime,timezone
from .market import MarketError, discover_symbols, discover_contract_details
from .strategy_lab import fetch_candles, direction_at

# Forex pairs visibly offered in Deriv Trader; runtime symbols still MUST
# originate from active_symbols, not from this display-name allowlist.
FX_PAIRS=('EURUSD','GBPJPY','GBPAUD','AUDNZD','AUDCAD','NZDUSD',
          'GBPNZD','USDPLN','EURNZD','GBPCAD','GBPCHF','AUDCHF',
          'NZDJPY','USDMXN','USDCHF','EURJPY','GBPUSD','USDJPY',
          'AUDUSD','AUDJPY','EURAUD','EURGBP','USDCAD','EURCAD','EURCHF')
MAX_SYMBOLS=32
SCAN_BATCH=8
_scan_offset=0
# A scan may discover several independent signals at the same M1 close.
# Keep them briefly per user so the executor can submit each one in turn.
_pending_signals_by_user={}
EXPIRY={'range_break':3,'forex':15,'step':5,'volatility':5}
_catalog_cache=()
_catalog_at=0.0
_contract_cache={}
_last_scan={'status':'NOT_RUN','at':None,'assets':[],'selected':None}
_log=logging.getLogger('nexora.v2')


def has_pending_signals(user_key):
    """Whether another confirmed candidate from this scan is still fresh."""
    now=time.time()
    return any(0 <= now-(item['last_bar_epoch']+60) <= 50
               for item in _pending_signals_by_user.get(user_key,[]))


def last_scan_report():
    """Safe, read-only explanation of the last scan (no account credentials)."""
    return {'status':_last_scan['status'],'at':_last_scan['at'],
            'assets':[dict(a) for a in _last_scan['assets']],
            'selected':dict(_last_scan['selected']) if _last_scan['selected'] else None,
            'reason':_last_scan.get('reason'),
            'catalogue_size':_last_scan.get('catalogue_size'),
            'batch_size':_last_scan.get('batch_size'),
            'next_scan_offset':_last_scan.get('next_scan_offset')}



def classify(item):
    symbol=item.get('symbol','')
    label=item.get('display_name','')
    if not isinstance(symbol,str) or not isinstance(label,str):
        return None
    if not re.fullmatch(r'[A-Za-z0-9_]{2,30}',symbol):
        return None
    normalized=re.sub('[^A-Z]','',label.upper())
    code=symbol.upper()
    for pair in FX_PAIRS:
        if normalized==pair or (pair in normalized and ('FOREX' in normalized or 'CURRENCY' in normalized)) or code in (pair,'FRX'+pair):
            return 'forex',pair
    lower=label.lower()
    if 'range break' in lower:
        return 'range_break',symbol
    if 'step' in lower and 'skew' not in lower and 'multi' not in lower:
        return 'step',symbol
    vol=re.search(r'volatility\s*(10|25|50|75|100)\b',lower)
    if vol:
        return 'volatility',symbol
    return None


def choose_tradeable(symbols):
    """Only items returned by the Deriv catalogue, never assumed identifiers."""
    picked={}
    for item in symbols:
        if not isinstance(item,dict):
            continue
        kind=classify(item)
        if not kind or kind[0]!='forex':
            continue
        family,group=kind
        symbol=item['symbol']
        if symbol not in picked:
            picked[symbol]={**item,'family':family,'group':group}
    priority={'forex':0,'range_break':1,'step':2,'volatility':3}
    ordered=sorted(picked.values(),key=lambda item:(
        priority[item['family']],
        FX_PAIRS.index(item['group']) if item['family']=='forex' else 0,
        item['symbol']))
    return ordered[:MAX_SYMBOLS]


async def catalogue():
    global _catalog_at,_catalog_cache
    now=time.monotonic()
    if _catalog_cache and now-_catalog_at<300:
        return list(_catalog_cache)
    found=choose_tradeable(await discover_symbols())
    _catalog_cache=tuple(found)
    _catalog_at=now
    return found


def duration_seconds(value):
    """Convert Deriv contract duration limits (s/m/h/d) to seconds."""
    if not isinstance(value,str):
        return None
    match=re.fullmatch(r'([1-9][0-9]*)([smhd])',value)
    if not match:
        return None
    return int(match.group(1))*{'s':1,'m':60,'h':3600,'d':86400}[match.group(2)]


def contract_supports_duration(contracts, *, minutes):
    """Both CALL and PUT must explicitly permit the selected expiration."""
    seconds=minutes*60
    allowed=set()
    for contract in contracts:
        if contract.get('contract_type') not in ('CALL','PUT'):
            continue
        minimum=duration_seconds(contract.get('min_contract_duration'))
        maximum=duration_seconds(contract.get('max_contract_duration'))
        if minimum is not None and maximum is not None and minimum<=seconds<=maximum:
            allowed.add(contract['contract_type'])
    return {'CALL','PUT'}.issubset(allowed)


async def supported(symbol, minutes=3):
    now=time.monotonic()
    key=(symbol,minutes)
    cached=_contract_cache.get(key)
    if cached and now-cached[0]<300:
        return cached[1]
    contracts=await discover_contract_details(symbol)
    ok=contract_supports_duration(contracts,minutes=minutes)
    _contract_cache[key]=(now,ok)
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


async def select_demo_signal(*, exclude_symbols=(), user_key=None):
    """Select all distinct confirmed M1 candidates across sequential DEMO ticks."""
    global _last_scan,_scan_offset
    exclude=set(exclude_symbols)
    # Never reuse a setup after its 50-second post-candle freshness window.
    queued=_pending_signals_by_user.get(user_key,[])
    now=time.time()
    while queued:
        item=queued.pop(0)
        if item['symbol'] not in exclude and 0 <= now-(item['last_bar_epoch']+60) <= 50:
            return item
    stamp=datetime.now(timezone.utc).isoformat()
    try:
        items=await catalogue()
    except (MarketError,TimeoutError,OSError) as exc:
        _last_scan={'status':'CATALOG_UNAVAILABLE','at':stamp,'assets':[],
                    'selected':None,'reason':str(exc)[:160]}
        _log.warning('V2 selector: catalogue unavailable: %s',type(exc).__name__)
        return None
    if not items:
        _last_scan={'status':'NO_SUPPORTED_ASSETS_IN_CATALOG','at':stamp,
                    'assets':[],'selected':None}
        _log.warning('V2 selector: zero eligible symbols in Deriv public catalog')
        return None
    # Rotate a bounded scan batch across all eligible catalogue assets.
    # Do not contact dozens of WebSockets simultaneously on Render Free.
    total=len(items)
    selected_items=[items[(_scan_offset+i)%total]
                    for i in range(min(SCAN_BATCH,total))]
    _scan_offset=(_scan_offset+len(selected_items))%total
    sem=asyncio.Semaphore(3)
    async def examine(item):
        async with sem:
            symbol=item['symbol']
            if symbol in exclude:
                return None,{'symbol':symbol,'status':'OPEN_CONTRACT'}
            try:
                if not await supported(symbol,EXPIRY[item['family']]):
                    return None,{'symbol':symbol,'status':'CALL_PUT_OR_DURATION_UNAVAILABLE',
                                 'duration_minutes':EXPIRY[item['family']]}
                bars=await fetch_candles(symbol,count=120)
                candidate=signal_from_closed_candles(item,bars)
                if candidate is None:
                    return None,{'symbol':symbol,'status':'NO_FRESH_CONFIRMED_SIGNAL',
                                 'candles':len(bars)}
                return candidate,{'symbol':symbol,'status':'SIGNAL',
                                  'strategy':candidate['strategy'],
                                  'direction':candidate['direction']}
            except (MarketError,ValueError,TypeError,TimeoutError,OSError) as exc:
                return None,{'symbol':symbol,'status':'MARKET_DATA_ERROR',
                             'reason':str(exc)[:160]}
    try:
        result=await asyncio.wait_for(asyncio.gather(*(examine(i) for i in selected_items)),
                                      timeout=42)
    except TimeoutError:
        _last_scan={'status':'SCAN_TIMEOUT','at':stamp,'assets':[],
                    'selected':None,'reason':'Consultas Deriv excederam 42s'}
        _log.warning('V2 selector: public research requests exceeded 42 seconds')
        return None
    signals=[candidate for candidate,_ in result if candidate]
    signals.sort(key=lambda x:(0 if x['strategy']=='rompimento_m1' else 1,x['symbol']))
    chosen=signals[0] if signals else None
    _pending_signals_by_user[user_key]=signals[1:]
    rows=[row for _,row in result]
    status='SIGNAL' if chosen else (
        'MARKET_DATA_UNAVAILABLE' if all(x['status'] in (
            'MARKET_DATA_ERROR','CALL_PUT_OR_DURATION_UNAVAILABLE') for x in rows) else 'NO_SIGNAL')
    _last_scan={'status':status,'at':stamp,'assets':rows,
                'selected':chosen,'catalogue_size':total,'batch_size':len(rows),
                'next_scan_offset':_scan_offset}
    # Expose whether a feed failure was mistaken for a lack of setups.
    if status=='MARKET_DATA_UNAVAILABLE':
        _log.warning('V2 selector: no market data; results=%s',
                     ','.join(x['symbol']+':'+x['status'] for x in rows))
    return chosen

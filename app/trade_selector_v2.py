"""Experimental, DEMO-only candidate discovery from official Deriv public candles.

No historical-directional strategy is claimed profitable; broker DEMO settlements
provide the actual evaluation. Never invent symbols or buy from this module.
"""
from __future__ import annotations
import asyncio
import os
import re
import time
import logging
from datetime import datetime,timezone
from statistics import mean
from .market import MarketError, discover_symbols, discover_contract_details, public_request
from .strategy_lab import fetch_candles, direction_at

# Forex pairs visibly offered in Deriv Trader; runtime symbols still MUST
# originate from active_symbols, not from this display-name allowlist.
FX_PAIRS=('EURUSD','GBPJPY','GBPAUD','AUDNZD','AUDCAD','NZDUSD',
          'GBPNZD','USDPLN','EURNZD','GBPCAD','GBPCHF','AUDCHF',
          'NZDJPY','USDMXN','USDCHF','EURJPY','GBPUSD','USDJPY',
          'AUDUSD','AUDJPY','EURAUD','EURGBP','USDCAD','EURCAD','EURCHF')
MAX_SYMBOLS=32
# Each scanner pass covers the complete broker catalogue for the chosen market.
# Keep request concurrency bounded separately from the number of assets.
# A scan may discover several independent signals at the same M1 close.
# Keep them briefly per user so the executor can submit each one in turn.
_pending_signals_by_user={}
EXPIRY={'range_break':3,'forex':15,'step':5,'volatility':15}
_catalog_cache={}
_catalog_at={}
_contract_cache={}
_last_scan={'status':'NOT_RUN','at':None,'assets':[],'selected':None}
_log=logging.getLogger('nexora.v2')
# Temporary DEMO test; set NEXORA_V22_TEST_MODE=false to restore strict H1/M15.
V22_TEST_MODE=os.getenv('NEXORA_V22_TEST_MODE','true').lower()=='true'
_log.setLevel(logging.INFO)  # Explicitly emit low-volume scanner diagnostics on Render.


def clear_user_signals(user_key):
    """Drop cached signals when starting a fresh evaluation."""
    _pending_signals_by_user.pop(user_key,None)
    _pending_signals_by_user.pop((user_key,True),None)
    _pending_signals_by_user.pop((user_key,'pullback_v22'),None)
    _pending_signals_by_user.pop((user_key,'volatility_m1'),None)
    _pending_signals_by_user.pop((user_key,'volatility_v22'),None)


def active_market(now=None):
    """Route by the approved Forex research window; UTC is authoritative."""
    current=datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    if current.weekday()<5 and 8<=current.hour<17:
        return 'forex'
    return 'volatility'


def effective_strategy_mode(strategy_mode):
    # V2.2 users get a 24/7 read-only scanner with DEMO-only execution
    # still subject to existing broker contract and account safeguards.
    if strategy_mode=='pullback_v22':
        return 'pullback_v22' if active_market()=='forex' else 'volatility_v22'
    return strategy_mode


def pending_queue_key(user_key, filtered=False, strategy_mode=None):
    mode=effective_strategy_mode(strategy_mode)
    if mode in ('pullback_v22','volatility_v22'):
        return (user_key,mode)
    return (user_key,True) if filtered else user_key


def has_pending_signals(user_key,*,filtered=False,strategy_mode=None):
    now=time.time()
    queue_key=pending_queue_key(user_key,filtered,strategy_mode)
    return any(0 <= now-(item['last_bar_epoch']+item.get('bar_seconds',60)) <= 50
               for item in _pending_signals_by_user.get(queue_key,[]))


def last_scan_report():
    """Safe, read-only explanation of the last scan (no account credentials)."""
    return {'status':_last_scan['status'],'at':_last_scan['at'],
            'assets':[dict(a) for a in _last_scan['assets']],
            'selected':dict(_last_scan['selected']) if _last_scan['selected'] else None,
            'reason':_last_scan.get('reason'),
            'catalogue_size':_last_scan.get('catalogue_size'),
            'batch_size':_last_scan.get('batch_size'),
            'next_scan_offset':_last_scan.get('next_scan_offset'),
            'market':_last_scan.get('market')}



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
    # Some active_symbols 'brief' responses return codes instead of names.
    # Accept only known Volatility naming patterns actually supplied by Deriv.
    # Broker contract/type/duration checks still independently govern entries.
    if re.fullmatch(r'(?:1HZ[0-9]+V|R_[0-9]+)',symbol.upper()):
        return 'volatility',symbol
    vol=re.search(r'volatility\s*(10|15|25|30|50|75|90|100|150|200|250|300)\b',lower)
    if vol:
        return 'volatility',symbol
    return None


def choose_tradeable(symbols, *, market='forex'):
    """Use only real names/codes returned by Deriv, never hardcoded symbols."""
    picked={}
    for item in symbols:
        if not isinstance(item,dict):
            continue
        kind=classify(item)
        if not kind:
            continue
        family,group=kind
        if market=='forex' and family!='forex':
            continue
        if market=='volatility' and family!='volatility':
            continue
        symbol=item['symbol']
        picked.setdefault(symbol,{**item,'family':family,'group':group})
    if market=='forex':
        return sorted(picked.values(),key=lambda item:(
            FX_PAIRS.index(item['group']),item['symbol']))[:MAX_SYMBOLS]
    return sorted(picked.values(),key=lambda item:item['symbol'])[:MAX_SYMBOLS]


async def catalogue(market='forex'):
    global _catalog_at,_catalog_cache
    now=time.monotonic()
    cached=_catalog_cache.get(market)
    if cached and now-_catalog_at.get(market,0)<300:
        return list(cached)
    source=await discover_symbols()
    found=choose_tradeable(source,market=market)
    # Diagnose catalogue/label mismatches without storing account details.
    if not found:
        sample=[(item.get('symbol','')[:32],item.get('display_name','')[:55])
                for item in source[:8] if isinstance(item,dict)]
        _log.warning('V2 catalogue empty: market=%s source_count=%s sample=%s',
                     market,len(source),sample)
    _catalog_cache[market]=tuple(found)
    _catalog_at[market]=now
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


def filtered_breakout_ok(candles, direction):
    """Experimental V2.1 trend and breakout-range filters on COMPLETED M1 bars.

    Thresholds are research hypotheses; no assertion of profitability.
    """
    if len(candles)<55:
        return False
    tail=candles[-55:]
    closes=[float(bar['close']) for bar in tail]
    ranges=[float(bar['high'])-float(bar['low']) for bar in tail]
    avg_range=mean(ranges[-15:-1])
    current_range=ranges[-1]
    if avg_range<=0 or not 0.5*avg_range<=current_range<=2.5*avg_range:
        return False
    alpha=2/51
    ema=mean(closes[:50])
    previous_ema=ema
    for close in closes[50:]:
        previous_ema=ema
        ema=alpha*close+(1-alpha)*ema
    return ((direction=='CALL' and closes[-1]>ema and ema>previous_ema)
         or (direction=='PUT' and closes[-1]<ema and ema<previous_ema))


def signal_from_closed_candles(item,candles,now=None,*,filtered=False):
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
    if filtered and family=='forex' and not filtered_breakout_ok(tail,direction):
        return None
    return {'symbol':item['symbol'],'display_name':item['display_name'],
            'strategy':('rompimento_m1_filtrado_v21' if filtered and family=='forex' else
                        'rompimento_m1' if strategy=='range_channel' else 'tendencia_m1'),
            'direction':direction,'minutes':EXPIRY[family],
            'family':family,'last_bar_epoch':last['epoch'],
            'reason':('Rompimento M1 com filtro experimental EMA50 e amplitude 14 velas' if filtered and family=='forex' else
                      'Rompimento do canal de 20 velas M1 fechadas' if strategy=='range_channel'
                      else 'Cruzamento de médias 8/21 em velas M1 fechadas')}



def ema_values(closes, period=50):
    """EMA with an SMA seed using completed bars only."""
    if len(closes)<period+5:
        return []
    alpha=2/(period+1)
    value=mean(closes[:period])
    values=[value]
    for close in closes[period:]:
        value=alpha*close+(1-alpha)*value
        values.append(value)
    return values


def strict_closed_bars(message, granularity, *, now):
    """Validate actual Deriv OHLC, exclude unclosed bar, never fill gaps."""
    raw=message.get('candles')
    if not isinstance(raw,list):
        raise MarketError('Candles de mercado indisponíveis')
    result=[]
    for bar in raw:
        try:
            epoch=int(bar['epoch'])
            low=float(bar['low']); high=float(bar['high'])
            close=float(bar['close']); open_price=float(bar['open'])
        except (KeyError,ValueError,TypeError,OverflowError) as exc:
            raise MarketError('Candle inválido') from exc
        if not all(__import__('math').isfinite(x) and x>0 for x in (low,high,close,open_price)):
            raise MarketError('Preço não finito')
        if low>min(open_price,close) or high<max(open_price,close) or high<low:
            raise MarketError('OHLC incoerente')
        if epoch+granularity<=now:
            result.append({'epoch':epoch,'low':low,'high':high,
                           'close':close,'open':open_price})
    result.sort(key=lambda x:x['epoch'])
    if len({b['epoch'] for b in result})!=len(result):
        raise MarketError('Candles duplicados')
    return result


async def fetch_closed_timeframe(symbol, seconds, count, *, now):
    if seconds not in (900,3600):
        raise MarketError('Timeframe não autorizado')
    payload=await public_request({
        'ticks_history':symbol,'style':'candles','granularity':seconds,
        'count':count,'end':'latest'},expected='candles',timeout=18)
    return strict_closed_bars(payload,seconds,now=now)


def swing_structure(bars, direction, lookback=22):
    """Two most recent confirmed 2-left/2-right price pivots."""
    pivots_high=[];pivots_low=[]
    segment=bars[-lookback:]
    for i in range(2,len(segment)-2):
        if all(segment[i]['high']>segment[j]['high'] for j in (i-2,i-1,i+1,i+2)):
            pivots_high.append(segment[i]['high'])
        if all(segment[i]['low']<segment[j]['low'] for j in (i-2,i-1,i+1,i+2)):
            pivots_low.append(segment[i]['low'])
    if len(pivots_high)<2 or len(pivots_low)<2:
        return False
    if direction=='CALL':
        return pivots_high[-1]>pivots_high[-2] and pivots_low[-1]>pivots_low[-2]
    return pivots_high[-1]<pivots_high[-2] and pivots_low[-1]<pivots_low[-2]


def v22_pullback_signal(item, h1, m15, now):
    """Closed-candle H1 trend + M15 pullback confirmation for 15m Rise/Fall.

    This is a falsifiable research setup, not a profitable-strategy claim.
    """
    if len(h1)<85 or len(m15)<85:
        return None
    # Data gaps may occur on Forex weekends; require recent continuity only.
    if any(b['epoch']-a['epoch']!=3600 for a,b in zip(h1[-7:-1],h1[-6:])):
        return None
    if any(b['epoch']-a['epoch']!=900 for a,b in zip(m15[-19:-1],m15[-18:])):
        return None
    closed=m15[-1]['epoch']+900
    if not 0<=now-closed<=50:
        return None
    # Forex research session remains 08h-17h UTC on weekdays;
    # Volatility indices are 24/7 and use the SAME H1/M15 setup.
    moment=datetime.fromtimestamp(closed,timezone.utc)
    if item['family']=='forex' and not (moment.weekday()<5 and 8<=moment.hour<17):
        return None
    eh=ema_values([b['close'] for b in h1])
    em=ema_values([b['close'] for b in m15])
    if len(eh)<5 or len(em)<7:
        return None
    close_h=h1[-1]['close']
    if close_h>eh[-1] and eh[-1]>eh[-4]:
        direction='CALL'
    elif close_h<eh[-1] and eh[-1]<eh[-4]:
        direction='PUT'
    else:
        return None
    if V22_TEST_MODE:
        # Relax entry filters, not data integrity, official contract checks or risk guards.
        confirm=m15[-1]
        confirms=(confirm['close']>confirm['open'] and confirm['close']>em[-1]
                  if direction=='CALL' else
                  confirm['close']<confirm['open'] and confirm['close']<em[-1])
        if not confirms:
            return None
        return {'symbol':item['symbol'],'display_name':item['display_name'],
                'strategy':'h1_m15_test_relaxed_v22','direction':direction,
                'minutes':15,'family':item['family'],
                'last_bar_epoch':confirm['epoch'],'bar_seconds':900,
                'reason':'Teste DEMO: H1 EMA50 + confirmação M15 simplificada'}
    if not swing_structure(h1,direction):
        return None
    if direction=='CALL' and not (m15[-1]['close']>em[-1] and em[-1]>em[-4]):
        return None
    if direction=='PUT' and not (m15[-1]['close']<em[-1] and em[-1]<em[-4]):
        return None
    # Crossing EMA more than 2 times in the last 15 M15 closed candles => range.
    ema_tail=em[-15:]
    close_tail=[b['close'] for b in m15[-15:]]
    side=[1 if c>e else (-1 if c<e else 0) for c,e in zip(close_tail,ema_tail)]
    crosses=sum(side[i]!=0 and side[i-1]!=0 and side[i]!=side[i-1]
                for i in range(1,len(side)))
    if crosses>2:
        return None
    prev=m15[-2];confirm=m15[-1]
    bullish=confirm['close']>confirm['open'] and confirm['close']>prev['high']
    bearish=confirm['close']<confirm['open'] and confirm['close']<prev['low']
    if not (bullish if direction=='CALL' else bearish):
        return None
    # One to four closed candles before confirmation must have approached EMA50.
    # 35% of recent average true-candle range is an explicit research tolerance.
    ranges=[b['high']-b['low'] for b in m15[-15:-1]]
    avg_range=mean(ranges)
    if avg_range<=0:
        return None
    # Avoid abnormal confirmation candles where expiry risks chasing a spike.
    confirm_range=confirm['high']-confirm['low']
    if not 0.35*avg_range<=confirm_range<=2.5*avg_range:
        return None
    for offset in range(2,6):
        candle=m15[-offset]
        ema_at=em[-offset]
        near=(abs(candle['low']-ema_at)<=0.35*avg_range if direction=='CALL'
              else abs(candle['high']-ema_at)<=0.35*avg_range)
        if near:
            return {'symbol':item['symbol'],'display_name':item['display_name'],
                    'strategy':'h1_m15_pullback_v22','direction':direction,
                    'minutes':15,'family':item['family'],
                    'last_bar_epoch':confirm['epoch'],'bar_seconds':900,
                    'reason':('H1: EMA50 + swings; M15: EMA50 + pullback e confirmação; Forex UTC 08-17'
                              if item['family']=='forex' else
                              'H1: EMA50 + swings; M15: EMA50 + pullback e confirmação; Volatility 24/7')}
    return None


async def select_demo_signal(*, exclude_symbols=(), user_key=None, filtered=False, strategy_mode=None):
    """Select confirmed V2.2 H1/M15 candidates (or legacy M1) for DEMO."""
    global _last_scan
    exclude=set(exclude_symbols)
    # Never reuse a setup after its 50-second post-candle freshness window.
    effective_mode=effective_strategy_mode(strategy_mode)
    market='volatility' if effective_mode=='volatility_v22' else 'forex'
    queue_key=pending_queue_key(user_key,filtered,strategy_mode)
    queued=_pending_signals_by_user.get(queue_key,[])
    now=time.time()
    while queued:
        item=queued.pop(0)
        if item['symbol'] not in exclude and 0 <= now-(item['last_bar_epoch']+item.get('bar_seconds',60)) <= 50:
            return item
    stamp=datetime.now(timezone.utc).isoformat()
    try:
        items=await catalogue(market)
    except (MarketError,TimeoutError,OSError) as exc:
        _last_scan={'status':'CATALOG_UNAVAILABLE','at':stamp,'assets':[],
                    'selected':None,'reason':str(exc)[:160],'market':market}
        _log.warning('V2 selector: catalogue unavailable: %s',type(exc).__name__)
        return None
    if not items:
        _last_scan={'status':'NO_SUPPORTED_ASSETS_IN_CATALOG','at':stamp,
                    'assets':[],'selected':None,'market':market}
        _log.warning('V2 selector: zero eligible symbols in Deriv public catalog')
        return None
    # Evaluate EVERY currently eligible broker asset on each pass.
    # A semaphore keeps Deriv/WebSocket requests bounded on Render Free.
    total=len(items)
    selected_items=items
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
                if effective_mode in ('pullback_v22','volatility_v22'):
                    snapshot=time.time()
                    h1,m15=await asyncio.gather(
                        fetch_closed_timeframe(symbol,3600,105,now=snapshot),
                        fetch_closed_timeframe(symbol,900,115,now=snapshot))
                    candidate=v22_pullback_signal(item,h1,m15,snapshot)
                    candles_checked=len(m15)
                else:
                    bars=await fetch_candles(symbol,count=120)
                    candidate=(signal_from_closed_candles(item,bars,filtered=True) if filtered
                               else signal_from_closed_candles(item,bars))
                    candles_checked=len(bars)
                if candidate is None:
                    return None,{'symbol':symbol,'status':'NO_FRESH_CONFIRMED_SIGNAL',
                                 'candles':candles_checked}
                return candidate,{'symbol':symbol,'status':'SIGNAL',
                                  'strategy':candidate['strategy'],
                                  'direction':candidate['direction']}
            except (MarketError,ValueError,TypeError,TimeoutError,OSError) as exc:
                return None,{'symbol':symbol,'status':'MARKET_DATA_ERROR',
                             'reason':str(exc)[:160]}
    try:
        result=await asyncio.wait_for(asyncio.gather(*(examine(i) for i in selected_items)),
                                      timeout=90)
    except TimeoutError:
        _last_scan={'status':'SCAN_TIMEOUT','at':stamp,'assets':[],
                    'selected':None,'reason':'Consultas Deriv excederam 90s','market':market}
        _log.warning('V2 selector: public research requests exceeded 90 seconds')
        return None
    signals=[candidate for candidate,_ in result if candidate]
    signals.sort(key=lambda x:(0 if x['strategy']=='rompimento_m1' else 1,x['symbol']))
    chosen=signals[0] if signals else None
    _pending_signals_by_user[queue_key]=signals[1:]
    rows=[row for _,row in result]
    status='SIGNAL' if chosen else (
        'MARKET_DATA_UNAVAILABLE' if all(x['status'] in (
            'MARKET_DATA_ERROR','CALL_PUT_OR_DURATION_UNAVAILABLE') for x in rows) else 'NO_SIGNAL')
    _last_scan={'status':status,'at':stamp,'assets':rows,
                'selected':chosen,'catalogue_size':total,'batch_size':len(rows),
                'next_scan_offset':None,'market':market}
    # Summarize normal and failed scans so 'no signal' is distinguishable
    # from broken market data. No account info or private tokens are logged.
    from collections import Counter
    counts=dict(Counter(row['status'] for row in rows))
    if not hasattr(select_demo_signal,'_last_summary_at'):
        select_demo_signal._last_summary_at=0.0
    if status!='NO_SIGNAL' or time.monotonic()-select_demo_signal._last_summary_at>=60:
        _log.warning('V2 scan summary: market=%s mode=%s test_mode=%s assets=%s scanned=%s status=%s counts=%s',
                  market,effective_mode,V22_TEST_MODE,total,len(rows),status,counts)
        select_demo_signal._last_summary_at=time.monotonic()
    # Expose whether a feed failure was mistaken for a lack of setups.
    if status=='MARKET_DATA_UNAVAILABLE':
        _log.warning('V2 selector: no market data; results=%s',
                     ','.join(x['symbol']+':'+x['status'] for x in rows))
    return chosen

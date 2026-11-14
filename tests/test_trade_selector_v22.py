"""Deterministic, closed-bar-only checks for experimental H1/M15 Forex mode."""
from datetime import datetime,timezone,timedelta
from unittest.mock import AsyncMock
import asyncio

from app import trade_selector_v2 as selector


def bars(granularity, count, last_epoch):
    start=last_epoch-(count-1)*granularity
    candles=[]
    for i in range(count):
        c=1.1000+i*.0001
        candles.append({'epoch':start+i*granularity,
                        'open':c-.00004,'high':c+.00005,
                        'low':c-.00005,'close':c})
    return candles


def setup(monkeypatch):
    monkeypatch.setattr(selector,'SIGNAL_PROFILE','strict_v22')
    monkeypatch.setattr(selector,'V22_TEST_MODE',False)
    monkeypatch.setattr(selector,'active_market',lambda now=None:'forex')
    close=datetime(2026,11,13,12,0,tzinfo=timezone.utc).timestamp()
    h=bars(3600,100,int(close)-3600)
    m=bars(900,110,int(close)-900)
    e=selector.ema_values([c['close'] for c in m])
    # One prior candle has a pullback wick touching its *historical* EMA50.
    m[-3]['low']=e[-3]
    # Last closed bullish confirmation candle breaks the previous high.
    m[-1]['close']+=.0003
    m[-1]['high']=m[-1]['close']+.00005
    monkeypatch.setattr(selector,'swing_structure',lambda *a,**kw:True)
    item={'symbol':'frxEURUSD','display_name':'EUR/USD','family':'forex'}
    return item,h,m,close


def test_v22_valid_closed_h1_m15_pullback(monkeypatch):
    item,h,m,close=setup(monkeypatch)
    result=selector.v22_pullback_signal(item,h,m,close+5)
    assert result is not None
    assert result['strategy']=='h1_m15_pullback_v22'
    assert result['direction']=='CALL'
    assert result['minutes']==15
    assert result['bar_seconds']==900


def test_v22_no_early_or_stale_or_off_session_signal(monkeypatch):
    item,h,m,close=setup(monkeypatch)
    assert selector.v22_pullback_signal(item,h,m,close-1) is None
    assert selector.v22_pullback_signal(item,h,m,close+51) is None
    # 18:00 UTC is outside 08:00-17:00 and has the same synthetic price shape.
    shift=6*3600
    for b in h+m:
        b['epoch']+=shift
    assert selector.v22_pullback_signal(item,h,m,close+shift+5) is None


def test_v22_rejects_sideways_or_missing_pullback(monkeypatch):
    item,h,m,close=setup(monkeypatch)
    monkeypatch.setattr(selector,'swing_structure',lambda *a,**kw:False)
    assert selector.v22_pullback_signal(item,h,m,close+5) is None
    monkeypatch.setattr(selector,'swing_structure',lambda *a,**kw:True)
    m[-3]['low']=m[-3]['close']-.00005
    assert selector.v22_pullback_signal(item,h,m,close+5) is None


def test_v22_excludes_unclosed_candles():
    close=int(datetime(2026,11,13,12,0,tzinfo=timezone.utc).timestamp())
    payload={'candles':[{'epoch':close-900,'open':'1.101','close':'1.102',
                         'high':'1.103','low':'1.100'},
                        {'epoch':close,'open':'1.102','close':'1.103',
                         'high':'1.104','low':'1.101'}]}
    result=selector.strict_closed_bars(payload,900,now=close+5)
    assert len(result)==1 and result[0]['epoch']==close-900


def test_v22_selector_isolated_queue_and_uses_h1_m15(monkeypatch):
    item,h,m,close=setup(monkeypatch)
    monkeypatch.setattr(selector,'_pending_signals_by_user',{})
    monkeypatch.setattr(selector.time,'time',lambda:close+5)
    monkeypatch.setattr(selector,'catalogue',AsyncMock(return_value=[item]))
    monkeypatch.setattr(selector,'supported',AsyncMock(return_value=True))
    async def fake_fetch(symbol,seconds,count,*,now):
        return h if seconds==3600 else m
    monkeypatch.setattr(selector,'fetch_closed_timeframe',fake_fetch)
    result=asyncio.run(selector.select_demo_signal(user_key=2022,
         strategy_mode='pullback_v22'))
    assert result is not None and result['strategy']=='h1_m15_pullback_v22'
    assert (2022,'pullback_v22') in selector._pending_signals_by_user
    assert 2022 not in selector._pending_signals_by_user
    selector.clear_user_signals(2022)
    assert (2022,'pullback_v22') not in selector._pending_signals_by_user


def test_v23_confluence_scores_and_uses_closed_bars(monkeypatch):
    item,h,m,close=setup(monkeypatch)
    # Breakout and swing should clear 70 even with the range filter absent.
    signal=selector.v23_confluence_signal(item,h,m,close+5)
    assert signal is not None
    assert signal['strategy']=='h1_m15_confluence_v23'
    assert signal['minutes']==15
    assert 70<=signal['score']<=100
    assert selector.v23_confluence_signal(item,h,m,close-1) is None
    assert selector.v23_confluence_signal(item,h,m,close+51) is None


def test_v23_requires_confirmed_m15_and_score(monkeypatch):
    item,h,m,close=setup(monkeypatch)
    # A bullish H1 alone is insufficient.
    m[-1]['open']=m[-1]['close']+.00001
    m[-1]['high']=m[-1]['open']+.00005
    assert selector.v23_confluence_signal(item,h,m,close+5) is None
    # Missing optional confluence must not yield a trade.
    item,h,m,close=setup(monkeypatch)
    monkeypatch.setattr(selector,'swing_structure',lambda *a,**kw:False)
    m[-1]['close']=m[-2]['close']+.00001
    m[-1]['high']=m[-1]['close']+.00002
    m[-3]['low']=m[-3]['close']-.00005
    assert selector.v23_confluence_signal(item,h,m,close+5) is None


def test_v23_scanner_selects_highest_score(monkeypatch):
    item,h,m,close=setup(monkeypatch)
    monkeypatch.setattr(selector,'SIGNAL_PROFILE','confluence_v23')
    monkeypatch.setattr(selector,'_pending_signals_by_user',{})
    monkeypatch.setattr(selector.time,'time',lambda:close+5)
    one=dict(item)
    two={**item,'symbol':'frxGBPUSD','display_name':'GBP/USD'}
    monkeypatch.setattr(selector,'catalogue',AsyncMock(return_value=[one,two]))
    monkeypatch.setattr(selector,'supported',AsyncMock(return_value=True))
    async def fake_fetch(symbol,seconds,count,*,now):
        return h if seconds==3600 else m
    monkeypatch.setattr(selector,'fetch_closed_timeframe',fake_fetch)
    result=asyncio.run(selector.select_demo_signal(user_key=400,
         strategy_mode='pullback_v22'))
    assert result is not None
    assert result['strategy']=='h1_m15_confluence_v23'
    assert result['score']>=70
    assert len(selector._pending_signals_by_user[(400,'pullback_v22')])==1


def test_v23_volatility_uses_m5_candles_and_five_minute_expiry(monkeypatch):
    item,h,_,close=setup(monkeypatch)
    item={'symbol':'1HZ100V','display_name':'Volatility 100 (1s)','family':'volatility'}
    # Construct a fresh 5-minute close using genuine 300-second spacing.
    m=bars(300,110,int(close)-300)
    em=selector.ema_values([c['close'] for c in m])
    m[-3]['low']=em[-3]
    m[-1]['close']+=.0003
    m[-1]['high']=m[-1]['close']+.00005
    result=selector.v23_confluence_signal(item,h,m,close+5)
    assert result is not None
    assert result['strategy']=='h1_m5_confluence_v23'
    assert result['minutes']==5
    assert result['bar_seconds']==300
    assert result['score']>=70
    assert selector.v23_confluence_signal(item,h,m,close-1) is None
    assert selector.v23_confluence_signal(item,h,m,close+51) is None
    assert selector.EXPIRY['volatility']==5
    assert selector.EXPIRY['forex']==15


def test_v23_volatility_selector_requests_m5_and_five_minute_contract(monkeypatch):
    item,h,_,close=setup(monkeypatch)
    item={'symbol':'1HZ100V','display_name':'Volatility 100 (1s)','family':'volatility'}
    m=bars(300,110,int(close)-300)
    em=selector.ema_values([c['close'] for c in m])
    m[-3]['low']=em[-3]
    m[-1]['close']+=.0003
    m[-1]['high']=m[-1]['close']+.00005
    monkeypatch.setattr(selector,'SIGNAL_PROFILE','confluence_v23')
    monkeypatch.setattr(selector,'active_market',lambda now=None:'volatility')
    monkeypatch.setattr(selector,'_pending_signals_by_user',{})
    monkeypatch.setattr(selector.time,'time',lambda:close+5)
    monkeypatch.setattr(selector,'catalogue',AsyncMock(return_value=[item]))
    check=AsyncMock(return_value=True)
    monkeypatch.setattr(selector,'supported',check)
    requests=[]
    async def fake_fetch(symbol,seconds,count,*,now):
        requests.append(seconds)
        return h if seconds==3600 else m
    monkeypatch.setattr(selector,'fetch_closed_timeframe',fake_fetch)
    signal=asyncio.run(selector.select_demo_signal(user_key=9821,
             strategy_mode='pullback_v22'))
    assert signal is not None
    assert signal['minutes']==5
    assert signal['strategy']=='h1_m5_confluence_v23'
    check.assert_awaited_once_with('1HZ100V',5)
    assert sorted(requests)==[300,3600]

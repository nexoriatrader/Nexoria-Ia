"""Single-asset 30-day read-only Deriv study. Never submits orders."""
from __future__ import annotations
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite, sqrt
from statistics import mean, pstdev
from time import time
from .market import MarketError, public_request

SYMBOL='1HZ100V'
BAR_SECONDS=300
DAYS=30
MIN_TEST_SIGNALS=100

async def history():
    cutoff=int(time())-DAYS*86400
    before=int(time())
    bars={}
    # 30 days of M5 candles require 8640 points; paginate rather than
    # misleadingly treating the latest 1000 as a month.
    for _ in range(12):
        response=await public_request({
            'ticks_history':SYMBOL,'style':'candles','granularity':BAR_SECONDS,
            'count':900,'end':str(before)
        },expected='candles',timeout=25)
        raw=response.get('candles')
        if not isinstance(raw,list) or not raw:
            raise MarketError('Deriv não forneceu candles para o estudo de 30 dias')
        earliest=before
        for item in raw:
            try:
                epoch=int(item['epoch'])
                o,h,l,c=(float(item[k]) for k in ('open','high','low','close'))
            except (KeyError,ValueError,TypeError,OverflowError):
                raise MarketError('Histórico Deriv inválido') from None
            if not all(isfinite(x) and x>0 for x in (o,h,l,c)) or not l<=min(o,c)<=max(o,c)<=h:
                raise MarketError('OHLC inconsistente no histórico') from None
            earliest=min(earliest,epoch)
            if cutoff<=epoch and epoch+BAR_SECONDS<=time():
                bars[epoch]={'epoch':epoch,'open':o,'high':h,'low':l,'close':c}
        if earliest<=cutoff:
            break
        if earliest>=before:
            raise MarketError('Paginação do histórico não avançou')
        before=earliest-1
    result=sorted(bars.values(),key=lambda x:x['epoch'])
    if len(result)<8000 or result[0]['epoch']>cutoff+3600:
        raise MarketError('Histórico de 30 dias incompleto na Deriv; análise não realizada')
    return result

def ema(closes,period):
    alpha=2/(period+1)
    e=mean(closes[:period])
    for x in closes[period:]:
        e=alpha*x+(1-alpha)*e
    return e

def signal(bars,i,method):
    # Every decision uses only the fully closed candles through index i.
    if i<60:return None
    recent=bars[i-59:i+1]
    closes=[b['close'] for b in recent]
    current=recent[-1]
    if method=='ema50_trend':
        e=ema(closes,50)
        previous=ema(closes[:-1],50)
        if current['close']>e and e>previous and current['close']>current['open']:
            return 'CALL'
        if current['close']<e and e<previous and current['close']<current['open']:
            return 'PUT'
    elif method=='bollinger_reversal':
        window=closes[-20:]
        mid=mean(window); dev=pstdev(window)
        if dev<=0:return None
        # Cross back INSIDE the band: do not try to catch an extreme blindly.
        prior=closes[-21:-1]
        p_mid=mean(prior);p_dev=pstdev(prior)
        if p_dev<=0:return None
        if closes[-2]<p_mid-2*p_dev and closes[-1]>mid-2*dev:return 'CALL'
        if closes[-2]>p_mid+2*p_dev and closes[-1]<mid+2*dev:return 'PUT'
    elif method=='channel_breakout':
        previous=recent[-21:-1]
        if current['close']>max(b['high'] for b in previous):return 'CALL'
        if current['close']<min(b['low'] for b in previous):return 'PUT'
    elif method=='random_control':
        return 'CALL' if sha256(str(current['epoch']).encode()).digest()[0]%2 else 'PUT'
    return None

def evaluate(bars,start,end,method):
    wins=losses=draws=0
    i=max(60,start)
    # Non-overlapping five-minute hypothetical positions.
    while i+1<end:
        if bars[i+1]['epoch']-bars[i]['epoch']!=BAR_SECONDS:
            i+=1;continue
        direction=signal(bars,i,method)
        if not direction:
            i+=1;continue
        delta=bars[i+1]['close']-bars[i]['close']
        if delta==0:draws+=1
        elif (delta>0)==(direction=='CALL'):wins+=1
        else:losses+=1
        i+=1
    total=wins+losses+draws
    # Indicative net expectancy on 88% hypothetical profit, 100% loss;
    # NOT the broker's historical payouts or settled contracts.
    expectancy=(0.88*wins-losses)/total if total else None
    rate=wins/(wins+losses) if wins+losses else None
    # Wilson 95% lower confidence bound of directional win rate.
    n=wins+losses
    lower=None
    if n:
        z=1.96;p=wins/n;d=1+z*z/n
        lower=(p+z*z/(2*n)-z*sqrt(p*(1-p)/n+z*z/(4*n*n)))/d
    return {'signals':total,'wins':wins,'losses':losses,'draws':draws,
            'win_rate_excluding_draws':round(rate,5) if rate is not None else None,
            'wilson_95_lower':round(lower,5) if lower is not None else None,
            'illustrative_ev_at_88pct':round(expectancy,5) if expectancy is not None else None,
            'minimum_100_test_signals':total>=MIN_TEST_SIGNALS}

async def run_single_asset_study():
    bars=await history()
    split=int(len(bars)*0.7)
    approaches=('ema50_trend','bollinger_reversal','channel_breakout','random_control')
    rows=[{'strategy':method,
           'training':evaluate(bars,0,split,method),
           'holdout':evaluate(bars,split,len(bars),method)}
          for method in approaches]
    return {'state':'RESEARCH_ONLY','symbol':SYMBOL,
            'display_name':'Volatility 100 (1s)',
            'candles':len(bars),'granularity_seconds':BAR_SECONDS,
            'period_from':datetime.fromtimestamp(bars[0]['epoch'],timezone.utc).isoformat(),
            'period_to':datetime.fromtimestamp(bars[-1]['epoch'],timezone.utc).isoformat(),
            'split':'70% treinamento / 30% validação cronológica',
            'strategies':rows,'orders_sent':0,'martingale':False,'cycles':False,
            'execution_enabled':False,'payout_actual_verified':False,
            'warning':('Direção de velas históricas não é liquidação de contrato Rise/Fall. '
                       'O retorno a 88% é apenas ilustrativo. Não inclui preço de execução, '
                       'atraso, empates, payout real ou variações intravela. '
                       'Não autoriza operação automática ou dinheiro real.')}

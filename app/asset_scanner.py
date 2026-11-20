"""Read-only multi-symbol scanner. It never sends orders or recommends entries.

All candidate labels are unvalidated research hypotheses, not a trading signal.
Only official public Deriv symbols/candles are requested.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime,timezone
from .market import MarketError,discover_symbols,discover_contracts
from .strategy_lab import fetch_candles, score, EXPIRIES, MIN_SIGNALS
from .autopilot import analyze

MAX_ASSETS=8
MAX_PARALLEL=3


def scanner_family(display_name: str) -> str | None:
    name=display_name.lower()
    if 'range break' in name:
        return 'range_break'
    if 'step' in name and 'skew' not in name and 'multi' not in name:
        return 'step'
    if re.search(r'volatility\s*(10|25|50|75|100)\b',name):
        return 'volatility'
    return None


def choose_assets(symbols: list[dict]) -> list[dict]:
    """Pick varied genuine catalogue instruments, not invented symbol IDs."""
    preferred=[]
    seen=set()
    for item in symbols:
        if not isinstance(item,dict):
            continue
        symbol=item.get('symbol')
        name=item.get('display_name','')
        if not isinstance(symbol,str) or not isinstance(name,str):
            continue
        if not re.fullmatch(r'[A-Za-z0-9_]{2,30}',symbol):
            continue
        family=scanner_family(name)
        if not family:
            continue
        # One instance per volatility level and one of each other family.
        match=re.search(r'volatility\s*(10|25|50|75|100)\b',name.lower())
        group='vol_'+match.group(1) if family=='volatility' and match else family
        if group in seen:
            continue
        seen.add(group)
        preferred.append({'symbol':symbol,'display_name':name[:100],'family':family})
        if len(preferred)>=MAX_ASSETS:
            break
    return preferred


def compare_history(candles: list[dict], family: str) -> dict:
    """Chronological, non-overlapping directional research, NEVER broker P/L.

    Expiry signals are evaluated only when the full observed interval stays
    inside their 70/30 partition; no lookahead or payout approximation.
    """
    if len(candles)<150:
        raise MarketError('Mínimo de 150 velas M1 para comparação histórica')
    if family not in ('step','range_break','volatility'):
        raise MarketError('Família de índice não reconhecida')
    # Same crossover hypothesis for Step and Volatility; Range Break uses
    # breakout channels. Neither is presumed to predict broker payouts.
    strategy='range_channel' if family=='range_break' else 'step_ma'
    split=int(len(candles)*0.7)
    rows=[]
    for minutes in EXPIRIES:
        training=score(candles,strategy,minutes,start=26,end=split-1)
        validation=score(candles,strategy,minutes,start=split,end=len(candles)-1)
        rows.append({'duration_minutes':minutes,'train':training,
                     'validation':validation,
                     'enough_validation_samples':validation['signals']>=MIN_SIGNALS,
                     'validated_for_trading':False})
    return {'strategy':strategy,'split':'70% treino / 30% validação cronológica',
            'results':rows,'contract_compatibility':'CALL_PUT_LISTED_DURATION_UNVERIFIED',
            'payout_adjusted':False,
            'warning':'Acertos DIRECIONAIS históricos, não lucro de contratos. '
                      'Payout e execução real não avaliados; sem estratégia liberada.'}


async def scan_assets() -> dict:
    symbols=await discover_symbols()
    targets=choose_assets(symbols)
    if not targets:
        return {'state':'NO_ASSETS','items':[],'errors':[],'orders_sent':0,
                'reason':'Nenhum índice compatível foi encontrado no catálogo público'}
    sem=asyncio.Semaphore(MAX_PARALLEL)

    async def inspect(item):
        async with sem:
            try:
                types=await discover_contracts(item['symbol'])
                if not {'CALL','PUT'}.issubset(set(types)):
                    return None, {'symbol':item['symbol'],
                                  'reason':'Contratos CALL/PUT não disponíveis no catálogo'}
                candles=await fetch_candles(item['symbol'],count=700)
                if len(candles)<150:
                    return None, {'symbol':item['symbol'],
                                  'reason':'Histórico M1 insuficiente para analisar'}
                recent=candles[-700:]
                # Never use the current, still-forming candle; fetch_candles filters it.
                if any(b['epoch']-a['epoch']!=60 for a,b in zip(recent,recent[1:])):
                    return None, {'symbol':item['symbol'],
                                  'reason':'Histórico M1 incompleto ou com intervalos'}
                snapshot=analyze([c['close'] for c in recent])
                evaluation=compare_history(recent,item['family'])
                return {**item,'candles':len(recent),
                        'last_candle_at':datetime.fromtimestamp(
                            recent[-1]['epoch'],timezone.utc).isoformat(),
                        'research_state':snapshot['state'],
                        'comparison':evaluation,
                        'candidates':snapshot['candidates'],
                        'reason':snapshot['reason'],
                        'compatible_types':['CALL','PUT'],
                        'execution_allowed':False},None
            except (MarketError,ValueError,TypeError) as exc:
                return None,{'symbol':item['symbol'],'reason':str(exc)}

    results=await asyncio.gather(*(inspect(item) for item in targets))
    items=[item for item,error in results if item is not None]
    errors=[error for item,error in results if error is not None]
    return {'state':'RESEARCH_ONLY','items':items,'errors':errors,
            'checked_assets':len(targets),'orders_sent':0,
            'auto_selection_enabled':False,
            'warning':'Hipóteses experimentais, não sinais validados. Sem execução, '
                      'sem estimativa de lucro, sem seleção automática de ativo.'}


async def scan_intelligence_markets(*, previous=None, now=None) -> dict:
    """I02 extension of the existing scanner; no separate trading scanner.

    The I01 facade owns the closed-candle reads. This explicit entrypoint
    preserves scan_assets() and every legacy consumer unchanged.
    """
    from .intelligence2.market_scanner import scan_market_opportunities
    return await scan_market_opportunities(previous=previous, now=now)


async def scan_intelligence_context(*, previous=None, expiry_minutes=5, now=None) -> dict:
    """I03 composition of the existing I02 scanner and shared I01 context.

    No duplicate scanner, no account input, no financial side effects.
    """
    import time
    from .intelligence2.timeframe_engine import enrich_i02_report
    source = await scan_intelligence_markets(previous=previous, now=now)
    observed_at = int(time.time() if now is None else now)
    return await enrich_i02_report(
        source, expiry_minutes=expiry_minutes, now=observed_at,
    )

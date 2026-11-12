"""Read-only multi-symbol scanner. It never sends orders or recommends entries.

All candidate labels are unvalidated research hypotheses, not a trading signal.
Only official public Deriv symbols/candles are requested.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime,timezone
from .market import MarketError,discover_symbols,discover_contracts
from .strategy_lab import fetch_candles
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
                candles=await fetch_candles(item['symbol'],count=240)
                if len(candles)<80:
                    return None, {'symbol':item['symbol'],
                                  'reason':'Histórico M1 insuficiente para analisar'}
                recent=candles[-240:]
                # Never use the current, still-forming candle; fetch_candles filters it.
                if any(b['epoch']-a['epoch']!=60 for a,b in zip(recent,recent[1:])):
                    return None, {'symbol':item['symbol'],
                                  'reason':'Histórico M1 incompleto ou com intervalos'}
                snapshot=analyze([c['close'] for c in recent])
                return {**item,'candles':len(recent),
                        'last_candle_at':datetime.fromtimestamp(
                            recent[-1]['epoch'],timezone.utc).isoformat(),
                        'research_state':snapshot['state'],
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

"""NEXORA AutoPilot: inspectable research hypotheses, not trading orders."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
from math import isfinite, sqrt
from statistics import mean, pstdev
from typing import Sequence

MIN_PRICES = 80

@dataclass(frozen=True)
class Candidate:
    strategy: str
    direction: str
    reason: str
    validated: bool = False


def _clean(prices: Sequence[float]) -> list[float]:
    clean = []
    for raw in prices[-250:]:
        try:
            p = float(raw)
        except (TypeError, ValueError, OverflowError):
            raise ValueError('Cotação inválida') from None
        if not isfinite(p) or p <= 0:
            raise ValueError('Cotação inválida')
        clean.append(p)
    return clean


def analyze(prices: Sequence[float]) -> dict:
    """Find hypotheses in a market snapshot; never manufacture win rates."""
    p = _clean(prices)
    if len(p) < MIN_PRICES:
        return {'state': 'INSUFFICIENT_DATA', 'samples': len(p),
                'candidates': [], 'reason': f'Necessárias {MIN_PRICES} cotações para pesquisa'}
    current = p[-1]
    short = mean(p[-12:]); long = mean(p[-55:])
    returns = [(b-a)/a for a,b in zip(p[-61:-1], p[-60:])]
    vol = pstdev(returns)
    candidates: list[Candidate] = []
    if vol < 1e-8:
        return {'state': 'NO_SIGNAL', 'samples': len(p), 'candidates': [],
                'reason': 'Mercado sem variação suficiente para análise'}
    gradient = (short-long)/long
    if abs(gradient) > max(0.0003, 0.7*vol):
        candidates.append(Candidate('tendencia','CALL' if gradient>0 else 'PUT',
                                    'Diferença entre médias recentes e históricas'))
    window = p[-42:]
    center=mean(window); dispersion=pstdev(window)
    if dispersion > 0:
        z=(current-center)/dispersion
        if abs(z)>=2.2:
            candidates.append(Candidate('reversao','PUT' if z>0 else 'CALL',
                                        'Preço distante da média recente'))
    old = p[-31:-1]
    if current > max(old)*(1+0.15*vol):
        candidates.append(Candidate('rompimento','CALL','Rompimento da máxima anterior'))
    elif current < min(old)*(1-0.15*vol):
        candidates.append(Candidate('rompimento','PUT','Rompimento da mínima anterior'))
    return {'state':'CANDIDATES_UNVALIDATED' if candidates else 'NO_SIGNAL',
            'samples':len(p),'candidates':[asdict(c) for c in candidates],
            'reason':'Hipóteses experimentais; nenhuma estratégia validada para compra automática'
                      if candidates else 'Sem condições para gerar hipótese'}


def wilson_lower_bound(wins:int,total:int,z:float=1.96)->float:
    if not isinstance(wins,int) or not isinstance(total,int) or total<=0 or wins<0 or wins>total:
        raise ValueError('Amostra inválida')
    p=wins/total; denom=1+z*z/total
    return (p+z*z/(2*total)-z*sqrt(p*(1-p)/total+z*z/(4*total*total)))/denom


def quote_assessment(ask_price:str,payout:str,*,wins:int|None=None,
                     total:int|None=None,min_samples:int=200)->dict:
    """Conservative preliminary EV research; inputs must be independently validated."""
    try:
        ask,pay=Decimal(str(ask_price)),Decimal(str(payout))
    except (InvalidOperation,ValueError,TypeError):
        raise ValueError('Cotação inválida') from None
    if not ask.is_finite() or not pay.is_finite() or ask<=0 or pay<0:
        raise ValueError('Cotação inválida')
    if min_samples<1:
        raise ValueError('Amostra mínima inválida')
    threshold=float(ask/pay) if pay else None
    result={'ask_price':str(ask),'payout':str(pay),
            'break_even_probability':round(threshold,6) if threshold is not None else None,
            'state':'UNVALIDATED','expected_value_lower_bound':None,
            'reason':'Histórico independente insuficiente para avaliar vantagem'}
    if wins is None or total is None:
        return result
    if total<min_samples:
        if total<0 or wins<0 or wins>total: raise ValueError('Amostra inválida')
        result['sample_size']=total
        return result
    lower=wilson_lower_bound(wins,total)
    ev=Decimal(str(lower))*pay-ask
    result.update({'sample_size':total,'lower_win_rate':round(lower,6),
                   'expected_value_lower_bound':str(ev.quantize(Decimal('0.0001'))),
                   'state':'RESEARCH_POSITIVE' if ev>0 else 'RESEARCH_REJECTED',
                   'reason':'Avaliação preliminar; requer validação fora da amostra'
                            if ev>0 else 'Sem evidência conservadora de retorno positivo'})
    return result

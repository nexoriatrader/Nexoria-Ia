"""Broker-confirmed DEMO progression. Never predicts or simulates settled contracts.

A cycle = initial attempt + configured Martingales. After all lose, the final
loss stake is multiplied by the cycle multiplier to seed the next cycle; advance
the *next* cycle's base by the cycle multiplier. Any win resets both levels.
A selected number of cycles means ADDITIONAL cycles after the initial cycle.
Orders remain one-at-a-time and unknown outcomes must block all progression.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CAP_PER_TRADE=Decimal('100.00')
CAP_SEQUENCE=Decimal('100.00')


class ProgressionHalt(Exception):
    pass


@dataclass(frozen=True)
class NextEntry:
    stake: Decimal
    cycle: int
    gale: int
    retry: bool
    direction: str | None = None
    symbol: str | None = None
    strategy: str | None = None


def decimal(value):
    try:
        d=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError) as exc:
        raise ProgressionHalt('Progressão com valor inválido') from exc
    if not d.is_finite() or d<1 or d.as_tuple().exponent<-2:
        raise ProgressionHalt('Multiplicador deve ser número a partir de 1,00 e ter no máximo duas casas decimais')
    return d


def next_entry(*, base, gale_enabled, gales, gale_multiplier, cycles, cycle_multiplier, orders, cycles_enabled=False):
    """Orders MUST be ordered by ID and scoped to this run via a persisted anchor.

    Each row: {state, pnl, direction, symbol, strategy, stake}.
    The result is a plan. DB and broker purchase checks execute independently.
    """
    base=decimal(base)
    gale_multiplier=decimal(gale_multiplier)
    cycle_multiplier=decimal(cycle_multiplier)
    if gale_multiplier<1 or cycle_multiplier<1:
        raise ProgressionHalt('Multiplicador inválido')
    if gales not in (0,1,2,3,4,5) or (not gale_enabled and gales!=0):
        raise ProgressionHalt('Quantidade de Martingale inválida')
    if cycles_enabled and cycles is not None and cycles not in (1,2,3,4,5):
        raise ProgressionHalt('Quantidade de ciclos inválida')
    cycle=gale=0
    total=Decimal('0')
    previous=None
    last_id=-1
    for row in orders:
        oid=int(row['id'])
        if oid<=last_id:
            raise ProgressionHalt('Histórico de operações fora de ordem')
        last_id=oid
        if row['state']!='SETTLED' or row['pnl'] is None:
            raise ProgressionHalt('Contrato anterior não liquidado; aguardando reconciliação')
        if row.get('direction') not in ('CALL','PUT') or not row.get('symbol'):
            raise ProgressionHalt('Dados de progressão incompletos')
        if row.get('stake') is None:
            raise ProgressionHalt('Valor anterior desconhecido')
        expected=(base*((cycle_multiplier*(gale_multiplier**gales))**cycle)*(gale_multiplier**gale)).quantize(
            Decimal('0.01'),rounding=ROUND_HALF_UP)
        actual=decimal(row['stake'])
        if actual!=expected:
            raise ProgressionHalt('Entrada do histórico diferente da progressão planejada')
        total+=actual
        if total>CAP_SEQUENCE:
            raise ProgressionHalt('Limite máximo da sequência atingido')
        try:
            pnl=Decimal(str(row['pnl']))
        except (InvalidOperation,ValueError,TypeError) as exc:
            raise ProgressionHalt('Resultado da operação inválido') from exc
        if not pnl.is_finite():
            raise ProgressionHalt('Resultado da operação inválido')
        if pnl>0:
            cycle=gale=0
            total=Decimal('0')
            previous=None
        elif gale<gales:
            gale+=1
            previous=row
        elif not cycles_enabled:
            cycle=gale=0
            total=Decimal('0')
            previous=None
        else:
            cycle+=1
            gale=0
            previous=row
            if cycles is not None and cycle>cycles:
                raise ProgressionHalt('Quantidade de ciclos concluída; robô pausado')
    stake=(base*((cycle_multiplier*(gale_multiplier**gales))**cycle)*(gale_multiplier**gale)).quantize(
        Decimal('0.01'),rounding=ROUND_HALF_UP)
    if stake>CAP_PER_TRADE or total+stake>CAP_SEQUENCE:
        raise ProgressionHalt('Progressão ultrapassaria o limite máximo de US$ 100 por sequência')
    return NextEntry(stake=stake,cycle=cycle,gale=gale,retry=previous is not None,
                     direction=previous['direction'] if previous else None,
                     symbol=previous['symbol'] if previous else None,
                     strategy=previous.get('strategy') if previous else None)

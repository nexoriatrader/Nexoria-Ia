"""Broker-confirmed DEMO progression. Never predicts or simulates settled contracts.

A cycle = initial attempt + configured Martingales. After all lose, the final
loss stake is multiplied by the cycle multiplier to seed the next cycle; advance
the *next* cycle's base by the cycle multiplier. Any win resets both levels.
A selected number of cycles means ADDITIONAL cycles after the initial cycle.
After the final loss, start a fresh base-stake sequence without disarming.
With no cycles selected, exhausting Martingale already resets to base.
Only Martingale reuses the previous signal. A new cycle awaits the next confirmed\nstrategy signal; uncertain orders keep their own symbol locked until reconciled.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP



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


def decimal(value, *, min_value=Decimal('1')):
    try:
        d=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError) as exc:
        raise ProgressionHalt('Progressão com valor inválido') from exc
    if not d.is_finite() or d<min_value or d.as_tuple().exponent<-2:
        raise ProgressionHalt('Multiplicador deve ser número a partir de 1,00 e ter no máximo duas casas decimais')
    return d


def next_entry(*, base, gale_enabled, gales, gale_multiplier, cycles, cycle_multiplier, orders,
               cycles_enabled=False, validate_stakes=True):
    """Orders MUST be ordered by ID and scoped to this run via a persisted anchor.

    Each row: {state, pnl, direction, symbol, strategy, stake}. Manual legacy
    runs may disable stake matching while still requiring official settlements.
    The result is a plan. DB and broker purchase checks execute independently.
    """
    base=decimal(base,min_value=Decimal('0.50'))
    gale_multiplier=decimal(gale_multiplier)
    cycle_multiplier=decimal(cycle_multiplier)
    if gale_multiplier<1 or cycle_multiplier<1:
        raise ProgressionHalt('Multiplicador inválido')
    if gales not in (0,1,2,3,4,5) or (not gale_enabled and gales!=0):
        raise ProgressionHalt('Quantidade de Martingale inválida')
    if cycles_enabled and cycles is not None and cycles not in (1,2,3,4,5):
        raise ProgressionHalt('Quantidade de ciclos inválida')
    cycle=gale=0
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
        actual=decimal(row['stake'],min_value=Decimal('0.50'))
        if validate_stakes and actual!=expected:
            raise ProgressionHalt('Entrada do histórico diferente da progressão planejada')
        try:
            pnl=Decimal(str(row['pnl']))
        except (InvalidOperation,ValueError,TypeError) as exc:
            raise ProgressionHalt('Resultado da operação inválido') from exc
        if not pnl.is_finite():
            raise ProgressionHalt('Resultado da operação inválido')
        if pnl>0:
            cycle=gale=0
            previous=None
        elif gale<gales:
            gale+=1
            previous=row
        elif not cycles_enabled:
            cycle=gale=0
            previous=None
        else:
            cycle+=1
            gale=0
            previous=row
            if cycles is not None and cycle>cycles:
                # Exhausting the configured recovery sequence resets the stake
                # to its base for the NEXT independently confirmed signal.
                # Stop Win/Loss and the broker safeguards still apply.
                cycle=gale=0
                previous=None
    stake=(base*((cycle_multiplier*(gale_multiplier**gales))**cycle)*(gale_multiplier**gale)).quantize(
        Decimal('0.01'),rounding=ROUND_HALF_UP)
    # No arbitrary per-trade or cumulative sequence ceiling. Broker-defined
    # stake limits, available demo balance and user stops are checked at buy time.
    # A Martingale continues the losing contract on its ORIGINAL signal.
    # The FIRST entry of a new cycle is NOT a Martingale: it must await the
    # scanner's next confirmed signal, potentially on a different symbol.
    retry=previous is not None and gale>0
    return NextEntry(stake=stake,cycle=cycle,gale=gale,retry=retry,
                     direction=previous['direction'] if retry else None,
                     symbol=previous['symbol'] if retry else None,
                     strategy=previous.get('strategy') if retry else None)

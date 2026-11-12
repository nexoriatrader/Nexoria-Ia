"""No fabricated outcomes and safe multipliers for DEMO cycle progression."""
import pytest
from decimal import Decimal
from app.progression import next_entry,ProgressionHalt


def row(n,stake,pnl,kind='CALL'):
    return {'id':n,'state':'SETTLED','pnl':str(pnl),'stake':str(stake),
            'direction':kind,'symbol':'1HZ100V','strategy':'tendencia'}

def calculate(orders,**kw):
    defaults=dict(base='2.00',gale_enabled=True,gales=1,
                  gale_multiplier='2.0',cycles=1,cycle_multiplier='2.0',cycles_enabled=True)
    defaults.update(kw)
    return next_entry(orders=orders,**defaults)

def test_cycle_progression_2_4_8_16_as_requested():
    a=calculate([])
    assert (a.stake,a.cycle,a.gale)==(Decimal('2.00'),0,0)
    a=calculate([row(1,2,-2)])
    assert (a.stake,a.cycle,a.gale,a.direction)==(Decimal('4.00'),0,1,'CALL')
    a=calculate([row(1,2,-2),row(2,4,-4)])
    assert (a.stake,a.cycle,a.gale)==(Decimal('8.00'),1,0)
    a=calculate([row(1,2,-2),row(2,4,-4),row(3,8,-8)])
    assert (a.stake,a.cycle,a.gale)==(Decimal('16.00'),1,1)
    with pytest.raises(ProgressionHalt,match='ciclos concluída'):
        calculate([row(1,2,-2),row(2,4,-4),row(3,8,-8),row(4,16,-16)])

def test_win_resets_base_and_cycle():
    orders=[row(1,2,-2),row(2,4,-4),row(3,8,6)]
    next_result=calculate(orders)
    assert (next_result.stake,next_result.cycle,next_result.gale,next_result.retry)==(Decimal('2'),0,0,False)

def test_different_multipliers_can_be_entered():
    orders=[row(1,2,-2)]
    a=calculate(orders,gale_multiplier='2.3',cycle_multiplier='2.5')
    assert a.stake==Decimal('4.60')
    a=calculate(orders+[row(2,'4.60',-4.6)],gale_multiplier='2.3',cycle_multiplier='2.5')
    assert a.stake==Decimal('5.00')

def test_up_to_five_gales_and_cycles():
    a=calculate([],gales=5,cycles=5,gale_multiplier='1.0',cycle_multiplier='1.0')
    assert a.stake==Decimal('2.00')
    orders=[row(i,2,-2) for i in range(1,7)]
    a=calculate(orders,gales=5,cycles=5,gale_multiplier='1.0',cycle_multiplier='1.0')
    assert (a.cycle,a.gale)==(1,0)

def test_rejected_outcome_and_exposure_limits():
    with pytest.raises(ProgressionHalt,match='não liquidado'):
        calculate([dict(row(1,2,-2),state='CONFIRMED')])
    with pytest.raises(ProgressionHalt,match='histórico diferente'):
        calculate([row(1,4,-4)])
    with pytest.raises(ProgressionHalt,match='limite máximo'):
        calculate([row(i,2,-2) for i in range(1,50)],
                  gales=5,cycles=5,gale_multiplier='1',cycle_multiplier='1')
    with pytest.raises(ProgressionHalt):
        calculate([],cycle_multiplier='NaN')
    with pytest.raises(ProgressionHalt):
        calculate([],gale_multiplier='0.9')
    with pytest.raises(ProgressionHalt):
        calculate([],gales=6)

def test_cycles_disabled_stay_flat_after_exhausted_gale():
    a=calculate([row(1,2,-2),row(2,4,-4)],cycles_enabled=False)
    assert a.stake==Decimal('2.00')

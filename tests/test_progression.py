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
    # Both configured cycles have lost: new confirmed signal restarts at base.
    exhausted=[row(1,2,-2),row(2,4,-4),row(3,8,-8),row(4,16,-16)]
    restarted=calculate(exhausted)
    assert (restarted.stake,restarted.cycle,restarted.gale,restarted.retry)==(Decimal('2.00'),0,0,False)
    # The new sequence can progress again after the reset.
    after_reset=calculate(exhausted+[row(5,2,-2)])
    assert (after_reset.stake,after_reset.cycle,after_reset.gale,after_reset.retry)==(Decimal('4.00'),0,1,True)

def test_win_resets_base_and_cycle():
    orders=[row(1,2,-2),row(2,4,-4),row(3,8,6)]
    next_result=calculate(orders)
    assert (next_result.stake,next_result.cycle,next_result.gale,next_result.retry)==(Decimal('2'),0,0,False)

def test_different_multipliers_can_be_entered():
    orders=[row(1,2,-2)]
    a=calculate(orders,gale_multiplier='2.3',cycle_multiplier='2.5')
    assert a.stake==Decimal('4.60')
    a=calculate(orders+[row(2,'4.60',-4.6)],gale_multiplier='2.3',cycle_multiplier='2.5')
    assert a.stake==Decimal('11.50')

def test_up_to_five_gales_and_cycles():
    a=calculate([],gales=5,cycles=5,gale_multiplier='1.0',cycle_multiplier='1.0')
    assert a.stake==Decimal('2.00')
    orders=[row(i,2,-2) for i in range(1,7)]
    a=calculate(orders,gales=5,cycles=5,gale_multiplier='1.0',cycle_multiplier='1.0')
    assert (a.cycle,a.gale)==(1,0)

def test_rejected_outcome_and_no_arbitrary_stake_caps():
    with pytest.raises(ProgressionHalt,match='não liquidado'):
        calculate([dict(row(1,2,-2),state='CONFIRMED')])
    with pytest.raises(ProgressionHalt,match='histórico diferente'):
        calculate([row(1,4,-4)])
    after_six_losses=calculate([row(i,20,-20) for i in range(1,7)],
                               base='20.00',gales=5,cycles=5,
                               gale_multiplier='1',cycle_multiplier='1')
    assert after_six_losses.stake==Decimal('20.00')
    assert after_six_losses.cycle==1
    large_first_gale=calculate([row(1,50,-50)],base='50.00',
                               gale_multiplier='2.50',gales=2,cycles=0,
                               cycles_enabled=False)
    assert large_first_gale.stake==Decimal('125.00')
    large_second_gale=calculate([row(1,50,-50),row(2,125,-125)],
                                base='50.00',gale_multiplier='2.50',
                                gales=2,cycles=0,cycles_enabled=False)
    assert large_second_gale.stake==Decimal('312.50')
    with pytest.raises(ProgressionHalt):
        calculate([],cycle_multiplier='NaN')
    with pytest.raises(ProgressionHalt):
        calculate([],gale_multiplier='0.9')
    with pytest.raises(ProgressionHalt):
        calculate([],gales=6)

def test_cycles_disabled_stay_flat_after_exhausted_gale():
    a=calculate([row(1,2,-2),row(2,4,-4)],cycles_enabled=False)
    assert a.stake==Decimal('2.00')


def test_cycles_work_without_martingale():
    # With no gales, a LOSS completes the current cycle and increases the
    # next cycle's stake; a WIN still returns to the original amount.
    only_cycles=dict(base='1.00',gale_enabled=False,gales=0,
                     gale_multiplier='2.50',cycles=2,cycles_enabled=True,
                     cycle_multiplier='2.50')
    first=next_entry(orders=[],**only_cycles)
    assert (first.stake,first.cycle,first.gale)==(Decimal('1.00'),0,0)
    second=next_entry(orders=[row(1,1,-1)],**only_cycles)
    assert (second.stake,second.cycle,second.gale)==(Decimal('2.50'),1,0)
    third=next_entry(orders=[row(1,1,-1),row(2,'2.50',-2.50)],**only_cycles)
    assert (third.stake,third.cycle,third.gale)==(Decimal('6.25'),2,0)
    reset=next_entry(orders=[row(1,1,-1),row(2,'2.50',2.20)],**only_cycles)
    assert (reset.stake,reset.cycle,reset.gale)==(Decimal('1.00'),0,0)
    exhausted=[row(1,1,-1),row(2,'2.50',-2.5),row(3,'6.25',-6.25)]
    continued=next_entry(orders=exhausted,**only_cycles)
    assert (continued.stake,continued.cycle,continued.gale,continued.retry)==(Decimal('1.00'),0,0,False)
    # No frozen state: next losing entry can start a second sequence.
    continued_again=next_entry(orders=exhausted+[row(4,1,-1)],**only_cycles)
    assert (continued_again.stake,continued_again.cycle,continued_again.gale,continued_again.retry)==(Decimal('2.50'),1,0,False)


def test_martingale_only_exhaustion_resets_without_pausing():
    mg_only=dict(base='1.00',gale_enabled=True,gales=2,
                 gale_multiplier='2.50',cycles=0,cycles_enabled=False,
                 cycle_multiplier='1')
    losing=[row(1,1,-1),row(2,'2.50',-2.5),row(3,'6.25',-6.25)]
    result=next_entry(orders=losing,**mg_only)
    assert (result.stake,result.cycle,result.gale,result.retry)==(Decimal('1.00'),0,0,False)
    next_signal=next_entry(orders=losing+[row(4,1,-1)],**mg_only)
    assert (next_signal.stake,next_signal.cycle,next_signal.gale,next_signal.retry)==(Decimal('2.50'),0,1,True)


def test_exhausted_cycle_does_not_hide_unsettled_broker_order():
    config=dict(base='1.00',gale_enabled=False,gales=0,gale_multiplier='1',
                cycles=2,cycles_enabled=True,cycle_multiplier='2.50')
    rows=[row(1,1,-1),row(2,'2.50',-2.5),
          dict(row(3,'6.25',-6.25),state='CONFIRMED',pnl=None)]
    with pytest.raises(ProgressionHalt,match='não liquidado'):
        next_entry(orders=rows,**config)

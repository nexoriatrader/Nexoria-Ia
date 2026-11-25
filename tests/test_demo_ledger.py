from types import SimpleNamespace
from decimal import Decimal
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
import pytest
from app.db import Base, User, DemoOrder
from app.demo_ledger import (reserve, confirmed, settle, unknown, order_guard,
    OrderGuardError, link_recovered_contract, mark_broker_absent)
from app.demo import _validate_demo_url, DemoTradingError

@pytest.fixture
def setup_db():
    engine=create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        u=User(deriv_account_id='VDEMO',stake='2.00',stop_win='20',stop_loss='10',martingale=False)
        s.add(u); s.commit(); uid=u.id
    yield engine,uid
    engine.dispose()

def test_replay_does_not_buy_twice(setup_db):
    engine,uid=setup_db
    with Session(engine) as s:
        u=s.get(User,uid)
        o,created=reserve(s,u,request_key='stable-key-1',symbol='1HZ100V',direction='CALL',minutes=5)
        assert created
        again,created=reserve(s,u,request_key='stable-key-1',symbol='1HZ100V',direction='CALL',minutes=5)
        assert not created and again.id==o.id
        with pytest.raises(OrderGuardError, match='Contrato demo em andamento'):
            reserve(s,u,request_key='stable-key-2',symbol='1HZ100V',direction='PUT',minutes=5)

def test_settlement_and_max_loss(setup_db):
    engine,uid=setup_db
    with Session(engine) as s:
        u=s.get(User,uid)
        for i in range(5):
            order,_=reserve(s,u,request_key=f'loss-{i}-1',symbol='1HZ100V',direction='CALL',minutes=5)
            confirmed(s,order,{'contract_id':str(100+i),'buy_price':'2'})
            assert not settle(s,order,{'contract_id':str(100+i),'is_sold':0})
            assert settle(s,order,{'contract_id':str(100+i),'is_sold':1,'profit':'-2','status':'lost'})
            assert order.active_slot is None
        with pytest.raises(OrderGuardError,match='Stop Loss'):
            reserve(s,u,request_key='extra-loss',symbol='1HZ100V',direction='CALL',minutes=5)

def test_unknown_is_fail_closed(setup_db):
    engine,uid=setup_db
    with Session(engine) as s:
        u=s.get(User,uid)
        order,_=reserve(s,u,request_key='failed-net',symbol='1HZ100V',direction='CALL',minutes=5)
        unknown(s,order)
        with pytest.raises(OrderGuardError,match='pendente de reconciliação'):
            reserve(s,u,request_key='retry-other',symbol='1HZ100V',direction='CALL',minutes=5)

def test_recovery_links_unique_contract_and_preserves_absent_audit_row(setup_db):
    engine,uid=setup_db
    with Session(engine) as s:
        u=s.get(User,uid)
        order,_=reserve(s,u,request_key='recovery-match-1',symbol='1HZ100V',direction='CALL',minutes=5)
        unknown(s,order)
        link_recovered_contract(s,order,{
            'contract_id':'812345','underlying_symbol':'1HZ100V',
            'contract_type':'CALL','buy_price':'1.00','purchase_time':1764090000,
        })
        assert order.state=='CONFIRMED' and order.contract_id=='812345'
        assert order.active_slot==u.id
        assert order.broker_status=='recovered'
        assert settle(s,order,{'contract_id':'812345','is_sold':1,'profit':'0.50'})

        absent,_=reserve(s,u,request_key='recovery-absent-1',symbol='1HZ100V',direction='PUT',minutes=5)
        unknown(s,absent)
        with pytest.raises(OrderGuardError,match='não foi confirmada'):
            mark_broker_absent(s,absent)
        assert absent.active_slot==u.id and absent.state=='UNKNOWN'
        assert mark_broker_absent(s,absent,verified=True)
        assert absent.state=='CANCELLED' and absent.active_slot is None
        assert s.get(DemoOrder,absent.id) is not None
        replacement,created=reserve(s,u,request_key='recovery-retry-1',symbol='1HZ100V',direction='PUT',minutes=5)
        assert created and replacement.id!=absent.id

def test_reject_foreign_contract_or_nonfinite_pnl(setup_db):
    engine,uid=setup_db
    with Session(engine) as s:
        u=s.get(User,uid)
        o,_=reserve(s,u,request_key='safe-id-1',symbol='1HZ100V',direction='CALL',minutes=5)
        confirmed(s,o,{'contract_id':'9090','buy_price':'2'})
        with pytest.raises(OrderGuardError,match='não corresponde'):
            settle(s,o,{'contract_id':'9091','is_sold':1,'profit':'1'})
        with pytest.raises(OrderGuardError,match='inválido'):
            settle(s,o,{'contract_id':'9090','is_sold':1,'profit':'NaN'})
        assert o.state=='CONFIRMED'

@pytest.mark.parametrize('url',[
    'ws://api.derivws.com/trading/v1/options/ws/demo?otp=x',
    'wss://api.derivws.com.evil.net/trading/v1/options/ws/demo?otp=x',
    'wss://evil.net/trading/v1/options/ws/demo?otp=x',
    'wss://api.derivws.com/trading/v1/options/ws/demo/extra?otp=x',
    'wss://api.derivws.com:444/trading/v1/options/ws/demo?otp=x',
    'wss://api.derivws.com/trading/v1/options/ws/demo',
])
def test_reject_untrusted_demo_urls(url):
    with pytest.raises(DemoTradingError): _validate_demo_url(url)

def test_martingale_blocked_until_validated(setup_db):
    engine,uid=setup_db
    with Session(engine) as s:
        u=s.get(User,uid);u.martingale=True
        with pytest.raises(OrderGuardError, match='Martingale indisponível'):
            reserve(s,u,request_key='gale-disabled',symbol='1HZ100V',direction='CALL',minutes=5)


def test_demo_does_not_enforce_arbitrary_thousand_dollar_stake_ceiling():
    from app.demo import _amount
    user=SimpleNamespace(id=7,stake='1250.00',stop_win='0',stop_loss='0',
                         martingale=False)
    assert order_guard(user,[],Decimal('1250.00'),
                       parallel=True,symbol='R_75')==Decimal('0')
    assert _amount('1250.00')==1250.0
    # Stops remain authoritative even without an arbitrary dollar ceiling.
    user.stop_loss='500'
    with pytest.raises(OrderGuardError, match='Stop Loss'):
        order_guard(user,[],Decimal('1250.00'),
                    parallel=True,symbol='R_75')

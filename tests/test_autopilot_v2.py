"""NEXORA 2.0 DEMO portfolio safety and per-symbol progression tests."""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app import main,db
from app.demo_ledger import reserve,confirmed,settle,OrderGuardError
from test_demopilot import prepare,cleanup


def test_parallel_exposure_and_symbol_lock(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='10.00')
    try:
        with factory() as sess:
            user=sess.get(db.User,uid)
            o,_=reserve(sess,user,request_key='parallel-0001',symbol='EURJPY',
                direction='CALL',minutes=3,parallel=True)
            confirmed(sess,o,{'contract_id':'990001','buy_price':'2'})
            with pytest.raises(OrderGuardError,match='neste ativo'):
                reserve(sess,user,request_key='parallel-0002',symbol='EURJPY',
                    direction='PUT',minutes=3,parallel=True)
            o2,_=reserve(sess,user,request_key='parallel-0003',symbol='EURUSD',
                direction='PUT',minutes=3,parallel=True)
            confirmed(sess,o2,{'contract_id':'990002','buy_price':'2'})
            assert o.active_slot!=o2.active_slot
            with pytest.raises(OrderGuardError,match='Máximo de 2'):
                reserve(sess,user,request_key='parallel-0004',symbol='GBPUSD',
                    direction='CALL',minutes=3,parallel=True)
            assert settle(sess,o,{'contract_id':'990001','is_sold':1,'profit':'-2','status':'lost'})
            assert settle(sess,o2,{'contract_id':'990002','is_sold':1,'profit':'1.50','status':'won'})
    finally:cleanup(engine)


def test_parallel_pending_and_unknown_block_all_other_symbols(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    try:
        with factory() as sess:
            user=sess.get(db.User,uid)
            reserve(sess,user,request_key='pending-0001',symbol='EURJPY',
                direction='CALL',minutes=3,parallel=True)
            with pytest.raises(OrderGuardError,match='pendente ou incerto'):
                reserve(sess,user,request_key='pending-0002',symbol='EURUSD',
                    direction='CALL',minutes=3,parallel=True)
    finally:cleanup(engine)


def test_martingale_recovery_keeps_symbol_and_original_direction(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='0.00')
    try:
        with factory() as sess:
            user=sess.get(db.User,uid)
            user.stop_win='0.00'
            sess.add(db.PilotValidationBatch(user_id=uid,after_order_id=0))
            sess.add(db.RobotRun(user_id=uid,after_order_id=0))
            sess.add(db.RobotPlan(user_id=uid,requested_martingale=True,
                gale_count=2,requested_multiplier='2.0'))
            for symbol,pnl in [('EURJPY','-2.00'),('EURUSD','1.56')]:
                order=db.DemoOrder(user_id=uid,request_key='test-'+symbol,
                    state='SETTLED',symbol=symbol,direction='CALL',
                    duration_minutes=3,stake='2.00',pnl=pnl)
                sess.add(order)
                sess.flush()
                sess.add(db.AutoPilotAttribution(order_id=order.id,user_id=uid,
                    strategy='rompimento_m1',direction='CALL',reason='teste'))
            sess.commit()
        with factory() as sess:
            user=sess.get(db.User,uid)
            orders=sess.scalars(select(db.DemoOrder).order_by(db.DemoOrder.id)).all()
            entry,last=main.v2_symbol_recovery(sess,user,orders,
                sess.get(db.RobotPlan,uid),None,sess.get(db.PilotValidationBatch,uid))
            assert entry.retry
            assert entry.symbol=='EURJPY'
            assert entry.direction=='CALL'
            assert entry.stake==Decimal('4.00')
            assert last.duration_minutes==3
    finally:cleanup(engine)


def test_v2_restart_keeps_old_records_out_of_evaluation(tmp_path,monkeypatch):
    monkeypatch.setenv('NEXORA_V2_ENABLED','true')
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    try:
        with factory() as sess:
            sess.add(db.DemoOrder(user_id=uid,request_key='legacy-0001',state='SETTLED',
                symbol='1HZ100V',direction='CALL',duration_minutes=5,stake='2.00',pnl='1.00'))
            sess.commit()
        assert client.post('/api/autopilot/start',
            json={'confirm':'START_DEMO_AUTOPILOT'}).status_code==200
        stats=client.get('/api/autopilot/v2/evaluation').json()
        assert stats['active']
        assert stats['submitted']==0
        assert stats['target']==50 and not stats['target_is_limit']
        assert client.get('/api/demo/orders').json()['items']==[]
    finally:cleanup(engine)


def test_v2_parallel_real_path_with_two_assets(tmp_path,monkeypatch):
    monkeypatch.setenv('NEXORA_V2_ENABLED','true')
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='20.00')
    mockbuy=AsyncMock(side_effect=[
        {'contract_id':'909001','buy_price':'2.00'},
        {'contract_id':'909002','buy_price':'2.00'}])
    monkeypatch.setattr(main,'demo_buy',mockbuy)
    monkeypatch.setattr(main,'demo_proposal',AsyncMock(return_value={
        'ask_price':'2.00','payout':'3.60'}))
    monkeypatch.setattr(main,'select_demo_signal',AsyncMock(side_effect=[
        {'symbol':'EURJPY','direction':'CALL','minutes':3,
         'strategy':'rompimento_m1','reason':'rompimento'},
        {'symbol':'EURUSD','direction':'PUT','minutes':3,
         'strategy':'rompimento_m1','reason':'rompimento'}]))
    monkeypatch.setattr(main,'market_connected',False)
    try:
        assert client.post('/api/autopilot/start',
            json={'confirm':'START_DEMO_AUTOPILOT'}).status_code==200
        async def run():
            await main.autopilot_once()
        asyncio.run(run())
        main.v2_last_scan[uid]=-1e12
        main.autopilot_last_attempt[uid]=-1e12
        asyncio.run(run())
        assert mockbuy.await_count==2
        with factory() as sess:
            orders=sess.scalars(select(db.DemoOrder).order_by(db.DemoOrder.id)).all()
            assert [o.symbol for o in orders]==['EURJPY','EURUSD']
            assert all(o.state=='CONFIRMED' for o in orders)
        assert client.get('/api/autopilot/v2/evaluation').json()['submitted']==2
    finally:
        main.v2_last_scan.clear()
        cleanup(engine)


def test_v2_martingale_enters_same_symbol_without_waiting_for_scan(tmp_path,monkeypatch):
    """EURJPY recovery can coexist with EURUSD, but must never overlap EURJPY."""
    monkeypatch.setenv('NEXORA_V2_ENABLED','true')
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='20.00')
    broker=AsyncMock(side_effect=[
        {'contract_id':'910001','buy_price':'2.00'},
        {'contract_id':'910002','buy_price':'2.00'},
        {'contract_id':'910003','buy_price':'4.00'}])
    proposals=AsyncMock(return_value={'ask_price':'2.00','payout':'3.60'})
    signals=AsyncMock(side_effect=[
        {'symbol':'EURJPY','direction':'CALL','minutes':3,
         'strategy':'rompimento_m1','reason':'rompimento validando'},
        {'symbol':'EURUSD','direction':'PUT','minutes':3,
         'strategy':'rompimento_m1','reason':'rompimento validando'}])
    monkeypatch.setattr(main,'demo_buy',broker)
    monkeypatch.setattr(main,'demo_proposal',proposals)
    monkeypatch.setattr(main,'select_demo_signal',signals)
    monkeypatch.setattr(main,'market_connected',False)
    try:
        with factory() as s:
            s.add(db.RobotPlan(user_id=uid,requested_martingale=True,gale_count=1,
                requested_multiplier='2.00'))
            s.commit()
        assert client.post('/api/autopilot/start',
            json={'confirm':'START_DEMO_AUTOPILOT'}).status_code==200
        async def tick():
            await main.autopilot_once()
        asyncio.run(tick())
        main.v2_last_scan[uid]=-1e12
        main.autopilot_last_attempt[uid]=-1e12
        asyncio.run(tick())
        with factory() as s:
            eurjpy=s.scalar(select(db.DemoOrder).where(db.DemoOrder.symbol=='EURJPY'))
            assert eurjpy is not None
            assert settle(s,eurjpy,{'contract_id':'910001','is_sold':1,
                'profit':'-2.00','status':'lost'})
        main.autopilot_last_attempt[uid]=-1e12
        asyncio.run(tick())
        assert broker.await_count==3
        assert proposals.await_count==2  # no redundant preliminary quote for gale
        assert signals.await_count==2  # gale does not wait for new scanner cycle
        with factory() as s:
            orders=s.scalars(select(db.DemoOrder).order_by(db.DemoOrder.id)).all()
            assert [(o.symbol,o.stake,o.state) for o in orders]==[
                ('EURJPY','2.00','SETTLED'),('EURUSD','2.00','CONFIRMED'),
                ('EURJPY','4.00','CONFIRMED')]
            attr=s.scalar(select(db.AutoPilotAttribution).where(
                db.AutoPilotAttribution.order_id==orders[-1].id))
            assert attr.strategy=='rompimento_m1'
        # No fourth trade while both symbols are occupied.
        main.autopilot_last_attempt[uid]=-1e12
        asyncio.run(tick())
        assert broker.await_count==3
    finally:
        main.v2_last_scan.clear()
        cleanup(engine)

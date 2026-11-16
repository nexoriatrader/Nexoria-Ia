"""NEXORA 2.0 DEMO portfolio safety and per-symbol progression tests."""
import asyncio
from decimal import Decimal
from datetime import datetime,timezone,timedelta
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
            o3,_=reserve(sess,user,request_key='parallel-0004',symbol='GBPUSD',
                direction='CALL',minutes=15,parallel=True)
            confirmed(sess,o3,{'contract_id':'990003','buy_price':'2'})
            o4,_=reserve(sess,user,request_key='parallel-0005',symbol='AUDNZD',
                direction='CALL',minutes=15,parallel=True)
            confirmed(sess,o4,{'contract_id':'990004','buy_price':'2'})
            o5,_=reserve(sess,user,request_key='parallel-0006',symbol='USDCAD',
                direction='CALL',minutes=15,parallel=True)
            confirmed(sess,o5,{'contract_id':'990005','buy_price':'2'})
            assert len([o for o in (o,o2,o3,o4,o5) if o.state=='CONFIRMED'])==5
            assert settle(sess,o,{'contract_id':'990001','is_sold':1,'profit':'-2','status':'lost'})
            assert settle(sess,o2,{'contract_id':'990002','is_sold':1,'profit':'1.50','status':'won'})
    finally:cleanup(engine)


def test_parallel_pending_and_unknown_lock_only_their_symbol(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    try:
        with factory() as sess:
            user=sess.get(db.User,uid)
            first,_=reserve(sess,user,request_key='pending-0001',symbol='EURJPY',
                direction='CALL',minutes=3,parallel=True)
            with pytest.raises(OrderGuardError,match='neste ativo'):
                reserve(sess,user,request_key='pending-0002',symbol='EURJPY',
                    direction='PUT',minutes=3,parallel=True)
            second,_=reserve(sess,user,request_key='pending-0003',symbol='EURUSD',
                direction='CALL',minutes=3,parallel=True)
            assert second.state=='PENDING'
            from app.demo_ledger import unknown
            unknown(sess,first)
            with pytest.raises(OrderGuardError,match='neste ativo'):
                reserve(sess,user,request_key='pending-0004',symbol='EURJPY',
                    direction='PUT',minutes=3,parallel=True)
            third,_=reserve(sess,user,request_key='pending-0005',symbol='GBPUSD',
                direction='CALL',minutes=3,parallel=True)
            assert third.state=='PENDING'
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


def test_recovery_queue_prefers_oldest_settled_loss_not_latest_id(tmp_path,monkeypatch):
    """An older unresolved Martingale must not be starved by newer losses."""
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='0.00')
    try:
        base=datetime(2026,11,16,13,0,tzinfo=timezone.utc)
        with factory() as sess:
            user=sess.get(db.User,uid)
            sess.add(db.PilotValidationBatch(user_id=uid,after_order_id=0))
            sess.add(db.RobotRun(user_id=uid,after_order_id=0))
            sess.add(db.RobotPlan(user_id=uid,requested_martingale=True,
                gale_count=2,requested_multiplier='2.50'))
            for i,(symbol,settled_at) in enumerate((
                ('EURUSD',base),('EURJPY',base+timedelta(minutes=10)),
                ('GBPUSD',base+timedelta(minutes=20)))):
                order=db.DemoOrder(user_id=uid,request_key='fair-'+symbol,
                    state='SETTLED',symbol=symbol,direction='CALL',
                    duration_minutes=15,stake='2.00',pnl='-2.00',
                    created_at=settled_at-timedelta(minutes=15),
                    updated_at=settled_at)
                sess.add(order)
                sess.flush()
                sess.add(db.AutoPilotAttribution(order_id=order.id,user_id=uid,
                    strategy='test_forex',direction='CALL',reason='teste'))
            sess.commit()
        with factory() as sess:
            user=sess.get(db.User,uid)
            plan=sess.get(db.RobotPlan,uid)
            batch=sess.get(db.PilotValidationBatch,uid)
            rows=sess.scalars(select(db.DemoOrder).order_by(db.DemoOrder.id)).all()
            entry,last=main.v2_symbol_recovery(sess,user,rows,plan,None,batch)
            assert (entry.symbol,entry.direction,entry.gale,entry.stake)==(
                'EURUSD','CALL',1,Decimal('5.00'))
            assert last.symbol=='EURUSD'
            # Once an earlier symbol is in an official open state, its
            # progression must pause without blocking another symbol.
            first=next(o for o in rows if o.symbol=='EURUSD')
            first.state='CONFIRMED'
            sess.flush()
            entry,last=main.v2_symbol_recovery(sess,user,rows,plan,None,batch)
            assert entry.symbol=='EURJPY'
            assert last.symbol=='EURJPY'
    finally:
        cleanup(engine)


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


def test_status_exposes_legacy_session_and_required_v2_rearm(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    try:
        monkeypatch.setenv('NEXORA_V2_ENABLED','false')
        assert client.post('/api/autopilot/start',
            json={'confirm':'START_DEMO_AUTOPILOT'}).status_code==200
        monkeypatch.setenv('NEXORA_V2_ENABLED','true')
        state=client.get('/api/status').json()
        assert state['autopilot_version']=='V1_SINGLE_ASSET'
        assert state['v2_upgrade_needed'] is True
        assert state['analysis_symbol']=='1HZ100V'
        assert client.post('/api/autopilot/stop').status_code==200
        assert client.post('/api/autopilot/start',
            json={'confirm':'START_DEMO_AUTOPILOT'}).status_code==200
        state=client.get('/api/status').json()
        assert state['autopilot_version']=='V2_MULTI_ASSET'
        assert state['v2_upgrade_needed'] is False
    finally:cleanup(engine)


def test_v2_scanner_diagnostics_distinguish_broker_failure_from_no_signal(tmp_path,monkeypatch):
    from app import trade_selector_v2 as selector
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    try:
        monkeypatch.setenv('NEXORA_V2_ENABLED','true')
        monkeypatch.setattr(selector,'catalogue',AsyncMock(return_value=[
            {'symbol':'frxEURJPY','display_name':'EUR/JPY','family':'forex'}]))
        monkeypatch.setattr(selector,'supported',AsyncMock(return_value=True))
        from app.market import MarketError
        monkeypatch.setattr(selector,'fetch_candles',
                            AsyncMock(side_effect=MarketError('Public feed disconnected')))
        assert asyncio.run(selector.select_demo_signal()) is None
        status=client.get('/api/autopilot/v2/scanner-status')
        assert status.status_code==200
        scan=status.json()['scan']
        assert scan['status']=='MARKET_DATA_UNAVAILABLE'
        assert scan['assets'][0]['status']=='MARKET_DATA_ERROR'
        assert scan['assets'][0]['symbol']=='frxEURJPY'
    finally:cleanup(engine)


def test_selector_expands_catalogue_without_inventing_symbols():
    from app import trade_selector_v2 as selector
    raw=[{'symbol':'frx'+pair,'display_name':pair[:3]+'/'+pair[3:]}
         for pair in selector.FX_PAIRS]
    raw += [{'symbol':f'R_{i}','display_name':f'Volatility {i} Index'}
            for i in (10,25,50,75,100)]
    raw += [{'symbol':f'1HZ{i}V','display_name':f'Volatility {i} (1s) Index'}
            for i in (10,25,50,75,100)]
    raw += [{'symbol':'STPRNG','display_name':'Step Index'},
            {'symbol':'RBX01','display_name':'Range Break 100 Index'}]
    raw += [{'symbol':'FAKE123','display_name':'Not a recognized market'}]
    out=selector.choose_tradeable(raw)
    assert len(out)>=20
    assert len(out)==len({x['symbol'] for x in out})
    assert all(x['symbol']!='FAKE123' for x in out)
    assert {'frxEURJPY','frxAUDUSD','frxAUDNZD','frxEURCAD'} <= {
        x['symbol'] for x in out}
    assert all(x['family']=='forex' for x in out)


def test_selector_rotates_bounded_groups_across_catalogue(monkeypatch):
    from app import trade_selector_v2 as selector
    monkeypatch.setattr(selector,'_scan_offset',0)
    items=[{'symbol':f'frx{pair}','display_name':pair,
            'family':'forex'} for pair in selector.FX_PAIRS]
    items += [{'symbol':f'R_{n}','display_name':f'Volatility {n} Index',
               'family':'volatility'} for n in (10,25,50,75,100)]
    monkeypatch.setattr(selector,'catalogue',AsyncMock(return_value=items))
    monkeypatch.setattr(selector,'supported',AsyncMock(return_value=False))
    first=asyncio.run(selector.select_demo_signal())
    report1=selector.last_scan_report()
    second=asyncio.run(selector.select_demo_signal())
    report2=selector.last_scan_report()
    assert first is None and second is None
    assert report1['catalogue_size']==len(items)
    assert report1['batch_size']==selector.SCAN_BATCH
    assert report2['batch_size']==selector.SCAN_BATCH
    seen1={row['symbol'] for row in report1['assets']}
    seen2={row['symbol'] for row in report2['assets']}
    assert not seen1.intersection(seen2)
    assert report1['status']=='MARKET_DATA_UNAVAILABLE'


def test_underscore_deriv_symbol_can_be_discovered_quoted_and_reserved(tmp_path,monkeypatch):
    from app import trade_selector_v2 as selector
    from app.demo import demo_proposal
    assert selector.classify({'symbol':'R_100','display_name':'Volatility 100 Index'}) == (
        'volatility','R_100')
    assert not selector.choose_tradeable([
        {'symbol':'R_100','display_name':'Volatility 100 Index'}])  # Forex-only M15 mode
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    try:
        with factory() as sess:
            user=sess.get(db.User,uid)
            order,created=reserve(sess,user,request_key='underscore001',
                symbol='R_100',direction='CALL',minutes=5)
            assert created and order.symbol=='R_100'
    finally:cleanup(engine)


def test_public_market_websocket_close_is_reported_as_data_error(monkeypatch):
    import sys,types
    from websockets.exceptions import ConnectionClosedError
    from app.market import public_request,MarketError
    from app import trade_selector_v2 as selector
    class ClosedSocket:
        async def __aenter__(self): raise ConnectionClosedError(None,None)
        async def __aexit__(self,*args): return False
    import websockets
    monkeypatch.setattr(websockets,'connect',lambda *a,**kw:ClosedSocket())
    try:
        asyncio.run(public_request({'active_symbols':'brief'},expected='active_symbols',timeout=1))
        assert False,'ConnectionClosedError must convert to MarketError'
    except MarketError as exc:
        assert 'ConnectionClosedError' in str(exc)


def test_v2_uses_official_call_put_expiry_limits():
    from app.trade_selector_v2 import duration_seconds,contract_supports_duration
    assert duration_seconds('15s')==15
    assert duration_seconds('3m')==180
    assert duration_seconds('1h')==3600
    assert duration_seconds('1d')==86400
    assert duration_seconds('not-duration') is None
    contracts=[
        {'contract_type':'CALL','min_contract_duration':'5m',
         'max_contract_duration':'1d'},
        {'contract_type':'PUT','min_contract_duration':'5m',
         'max_contract_duration':'1d'}]
    assert not contract_supports_duration(contracts,minutes=3)
    assert contract_supports_duration(contracts,minutes=5)
    # Incomplete broker metadata must fail closed.
    assert not contract_supports_duration([{'contract_type':'CALL'}],minutes=3)
    assert not contract_supports_duration([contracts[0]],minutes=5)


def test_forex_m15_selector_and_half_dollar_progression():
    from app import trade_selector_v2 as selector
    from app.progression import next_entry
    assert selector.EXPIRY['forex']==15
    assert selector.choose_tradeable([
        {'symbol':'frxAUDNZD','display_name':'AUD/NZD'},
        {'symbol':'R_100','display_name':'Volatility 100 Index'}])[0]['symbol']=='frxAUDNZD'
    entry=next_entry(base='0.50',gale_enabled=True,gales=1,
        gale_multiplier='2',cycles=None,cycle_multiplier='2',orders=[])
    assert entry.stake==Decimal('0.50')
    again=next_entry(base='0.50',gale_enabled=True,gales=1,
        gale_multiplier='2',cycles=None,cycle_multiplier='2',
        orders=[{'id':1,'state':'SETTLED','pnl':'-0.50','direction':'CALL',
                 'symbol':'frxAUDNZD','strategy':'rompimento_m1','stake':'0.50'}])
    assert again.stake==Decimal('1.00') and again.symbol=='frxAUDNZD'


def test_half_dollar_four_parallel_m15_orders_keep_exposure_guard(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='0.00')
    try:
        with factory() as sess:
            user=sess.get(db.User,uid)
            for i,symbol in enumerate(('frxAUDNZD','frxEURUSD','frxUSDJPY','frxGBPUSD')):
                o,_=reserve(sess,user,request_key=f'halfdollar-{i:04}',
                    symbol=symbol,direction='CALL',minutes=15,
                    stake_override=Decimal('0.50'),parallel=True)
                confirmed(sess,o,{'contract_id':str(100000+i),'buy_price':'0.50'})
            fifth,_=reserve(sess,user,request_key='halfdollar-0005',
                symbol='frxAUDCAD',direction='CALL',minutes=15,
                stake_override=Decimal('0.50'),parallel=True)
            confirmed(sess,fifth,{'contract_id':'100005','buy_price':'0.50'})
            assert fifth.state=='CONFIRMED'
    finally:cleanup(engine)


def test_v2_pending_signal_queue_exposes_other_assets_without_rescanning(monkeypatch):
    import time
    from app import trade_selector_v2 as selector
    monkeypatch.setattr(selector,'_pending_signals_by_user',{})
    monkeypatch.setattr(selector,'_scan_offset',0)
    now=int(time.time())//60*60
    monkeypatch.setattr(selector.time,'time',lambda: now+5)
    monkeypatch.setattr(selector,'catalogue',AsyncMock(return_value=[
        {'symbol':'frxEURUSD','family':'forex','display_name':'EUR/USD'},
        {'symbol':'frxAUDNZD','family':'forex','display_name':'AUD/NZD'}]))
    monkeypatch.setattr(selector,'supported',AsyncMock(return_value=True))
    monkeypatch.setattr(selector,'fetch_candles',AsyncMock(return_value=[{'epoch':i*60}
        for i in range(120)]))
    def fake_signal(item,bars):
        return {'symbol':item['symbol'],'display_name':item['display_name'],
            'direction':'CALL','minutes':15,'strategy':'rompimento_m1',
            'family':'forex','reason':'confirmação','last_bar_epoch':now-60}
    monkeypatch.setattr(selector,'signal_from_closed_candles',fake_signal)
    first=asyncio.run(selector.select_demo_signal(user_key=101))
    assert len(selector._pending_signals_by_user[101])==1, selector.last_scan_report()
    second=asyncio.run(selector.select_demo_signal(user_key=101,
        exclude_symbols=[first['symbol']]))
    assert first['symbol']!=second['symbol']
    assert selector.fetch_candles.await_count==2
    assert selector._pending_signals_by_user[101]==[]


def test_symbol_slots_fit_postgres_int32_and_are_distinct_per_pair():
    # The production demo_orders.active_slot column is INTEGER, not BIGINT.
    # Signed per-symbol slots must survive PostgreSQL insertion without DataError.
    from app.demo_ledger import _symbol_slot
    pairs=('frxAUDNZD','frxAUDCAD','frxEURUSD','frxGBPUSD','frxUSDJPY',
           'frxEURJPY','frxGBPNZD','frxGBPAUD')
    slots=[_symbol_slot(uid,pair) for uid in (1,2,3) for pair in pairs]
    assert all(-(2**31)<=slot<=2**31-1 for slot in slots)
    assert all(slot<0 for slot in slots)
    assert len(slots)==len(set(slots))
    assert _symbol_slot(1,'frxAUDNZD')==_symbol_slot(1,'frxAUDNZD')


def test_v22_new_evaluation_keeps_official_ledger_and_resets_only_score(tmp_path,monkeypatch):
    monkeypatch.setenv('NEXORA_V2_ENABLED','true')
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='0.00')
    try:
        with factory() as sess:
            user=sess.get(db.User,uid)
            user.stake='0.50'
            sess.add(db.RobotPlan(user_id=uid,requested_martingale=False,gale_count=0))
            sess.add(db.PilotValidationBatch(user_id=uid,after_order_id=0,max_orders=50))
            sess.add(db.DemoOrder(user_id=uid,request_key='historical-001',
                state='SETTLED',symbol='frxEURUSD',direction='CALL',
                duration_minutes=15,stake='2.00',pnl='-2.00'))
            sess.commit()
        assert client.get('/api/autopilot/v2/evaluation').json()['settled']==0
        forbidden=client.post('/api/autopilot/v2/new-evaluation',json={'confirm':'NO'})
        assert forbidden.status_code==403
        result=client.post('/api/autopilot/v2/new-evaluation',
            json={'confirm':'START_NEW_DEMO_EVALUATION'})
        assert result.status_code==200,result.text
        assert result.json()['running'] is False
        stats=client.get('/api/autopilot/v2/evaluation').json()
        assert stats['batch']=='NEXORA_2.2' and stats['target']==200
        assert (stats['submitted'],stats['settled'],stats['net_pnl'])==(0,0,'0')
        assert client.get('/api/demo/orders').json()['items']==[]
        with factory() as sess:
            assert len(sess.scalars(select(db.DemoOrder)).all())==1
            assert sess.get(db.User,uid).stake=='0.50'
            assert sess.get(db.RobotPlan,uid).requested_martingale is False
        assert client.post('/api/autopilot/start',
            json={'confirm':'START_DEMO_AUTOPILOT'}).status_code==200
        assert client.post('/api/autopilot/v2/new-evaluation',
            json={'confirm':'START_NEW_DEMO_EVALUATION'}).status_code==409
    finally:
        cleanup(engine)


def test_v21_reset_rejects_confirmed_contract(tmp_path,monkeypatch):
    monkeypatch.setenv('NEXORA_V2_ENABLED','true')
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='0.00')
    try:
        with factory() as sess:
            sess.add(db.DemoOrder(user_id=uid,request_key='unsettled-001',
                state='CONFIRMED',symbol='frxEURUSD',direction='CALL',
                duration_minutes=15,stake='0.50',contract_id='889012'))
            sess.commit()
        result=client.post('/api/autopilot/v2/new-evaluation',
            json={'confirm':'START_NEW_DEMO_EVALUATION'})
        assert result.status_code==409,result.text
        with factory() as sess:
            assert len(sess.scalars(select(db.DemoOrder)).all())==1
    finally:
        cleanup(engine)


def test_v2_symbol_history_out_of_order_uses_ascending_order_ids(tmp_path,monkeypatch):
    """SQL may return rows in any order; it must not stop a fixed-stake robot."""
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='0.00')
    try:
        with factory() as sess:
            user=sess.get(db.User,uid)
            user.stake='0.50'
            sess.add(db.RobotRun(user_id=uid,after_order_id=0))
            sess.add(db.PilotValidationBatch(user_id=uid,after_order_id=0,max_orders=100))
            for idx,pnl in enumerate(('-0.50','0.40','-0.50'),start=1):
                o=db.DemoOrder(user_id=uid,request_key=f'history-order-{idx}',
                    state='SETTLED',symbol='frxEURUSD',direction='CALL',
                    duration_minutes=15,stake='0.50',pnl=pnl)
                sess.add(o)
                sess.flush()
                sess.add(db.AutoPilotAttribution(order_id=o.id,user_id=uid,
                    strategy='rompimento_m1_filtrado_v21',direction='CALL',reason='teste'))
            sess.commit()
        with factory() as sess:
            user=sess.get(db.User,uid)
            rows=sess.scalars(select(db.DemoOrder).where(
                db.DemoOrder.user_id==uid).order_by(db.DemoOrder.id.desc())).all()
            assert len(rows)==3 and rows[0].id>rows[-1].id
            entry,last=main.v2_symbol_recovery(sess,user,rows,None,None,
                sess.get(db.PilotValidationBatch,uid))
            assert entry is None and last is None
            # It must also work with mixed symbols and an arbitrary shuffle.
            rows.reverse()
            entry,last=main.v2_symbol_recovery(sess,user,rows,None,None,
                sess.get(db.PilotValidationBatch,uid))
            assert entry is None and last is None
    finally:
        cleanup(engine)


def test_v24_recovery_waits_for_first_m5_after_broker_settlement():
    settled=datetime.fromtimestamp(3000-2,tz=timezone.utc)
    target,ready,warm=main.v24_recovery_timing(settled,now=3000-30)
    assert (target,ready,warm)==(3000,False,True)
    assert main.v24_recovery_timing(settled,now=3000+0.5)==(3000,True,False)
    assert main.v24_recovery_timing(settled,now=3000+3.01)==(3300,False,False)
    # The settlement itself happened AFTER opening, so no same-bar retry.
    settled_late=datetime.fromtimestamp(3000+0.2,tz=timezone.utc)
    assert main.v24_recovery_timing(settled_late,now=3000+1)==(3300,False,False)
    assert main.v24_recovery_timing(settled_late,now=3300-20)==(3300,False,True)
    assert main.v24_recovery_timing(settled_late,now=3300+2)==(3300,True,False)


def test_v24_recovery_does_not_advance_past_latest_settlement():
    settled=datetime.fromtimestamp(3280,tz=timezone.utc)
    target,ready,warm=main.v24_recovery_timing(settled,now=3270)
    assert (target,ready,warm)==(3300,False,True)
    target,ready,warm=main.v24_recovery_timing(settled,now=3300+1)
    assert (target,ready,warm)==(3300,True,False)


def test_deriv_purchase_time_is_distinct_from_signal_and_reservation(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='0.00')
    try:
        purchase_epoch=1794668102  # broker reports an actual second +2
        with factory() as sess:
            user=sess.get(db.User,uid)
            o,_=reserve(sess,user,request_key='broker-time-01',
                        symbol='R_75',direction='CALL',minutes=5,parallel=True)
            assert o.broker_purchase_at is None
            confirmed(sess,o,{'contract_id':'900100','buy_price':'0.50',
                              'broker_purchase_time':purchase_epoch})
            assert int(o.broker_purchase_at.timestamp())==purchase_epoch
            details=main.order_result_details(o)
            assert details['time_source']=='DERIV'
            assert details['broker_purchase_at'] is not None
            assert details['started_at_brasilia'] is not None
            assert details['started_at_brasilia']!=o.created_at.isoformat()
    finally:
        cleanup(engine)


def test_unknown_broker_purchase_time_must_not_use_local_created_at(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='0.00')
    try:
        with factory() as sess:
            user=sess.get(db.User,uid)
            o,_=reserve(sess,user,request_key='broker-time-02',
                        symbol='R_10',direction='PUT',minutes=5,parallel=True)
            confirmed(sess,o,{'contract_id':'900101','buy_price':'0.50'})
            details=main.order_result_details(o)
            assert details['broker_purchase_at'] is None
            assert details['started_at_brasilia'] is None
            assert details['time_source']=='NOT_REPORTED'
            assert settle(sess,o,{'contract_id':'900101','is_sold':1,
                                  'profit':'-0.50','date_start':1794668401})
            assert int(o.broker_purchase_at.timestamp())==1794668401
            assert main.order_result_details(o)['outcome']=='LOSS'
    finally:
        cleanup(engine)


def test_v24_forex_recovery_uses_m15_not_m5():
    settled=datetime.fromtimestamp(9000+12,tz=timezone.utc)
    assert main.v24_recovery_timing(settled,now=9900-30,bar_seconds=900)==(9900,False,True)
    assert main.v24_recovery_timing(settled,now=9900+2,bar_seconds=900)==(9900,True,False)
    assert main.v24_recovery_timing(settled,now=9900+3.01,bar_seconds=900)==(10800,False,False)
    with pytest.raises(ValueError):
        main.v24_recovery_timing(settled,now=9902,bar_seconds=60)


def test_cycle_only_uses_new_signal_any_symbol_and_resets_on_win(tmp_path,monkeypatch):
    """The next cycle is priced using the global confirmed ledger, not old symbol."""
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='0.00')
    try:
        with factory() as sess:
            user=sess.get(db.User,uid)
            sess.add(db.PilotValidationBatch(user_id=uid,after_order_id=0))
            sess.add(db.RobotRun(user_id=uid,after_order_id=0))
            sess.add(db.RobotPlan(user_id=uid,requested_martingale=False,
                gale_count=0,cycles_limit=2))
            sess.add(db.RobotCycleParams(user_id=uid,multiplier='2.50'))
            sess.commit()
            first=db.DemoOrder(user_id=uid,request_key='cycle-cross-001',
                state='SETTLED',symbol='EURUSD',direction='PUT',
                duration_minutes=5,stake=user.stake,pnl='-2.00')
            sess.add(first);sess.flush()
            sess.add(db.AutoPilotAttribution(order_id=first.id,user_id=uid,
                strategy='pulso_v2',direction='PUT',reason='fresh signal 1'))
            sess.commit()
        with factory() as sess:
            user=sess.get(db.User,uid)
            plan=sess.get(db.RobotPlan,uid)
            batch=sess.get(db.PilotValidationBatch,uid)
            cfg=sess.get(db.RobotCycleParams,uid)
            orders=sess.scalars(select(db.DemoOrder).order_by(db.DemoOrder.id)).all()
            entry,last=main.v2_symbol_recovery(sess,user,orders,plan,cfg,batch)
            assert (entry.cycle,entry.gale,entry.stake,entry.retry)==(
                1,0,Decimal('5.00'),False)
            assert entry.symbol is None and entry.direction is None
            assert last.symbol=='EURUSD'
            second=db.DemoOrder(user_id=uid,request_key='cycle-cross-002',
                state='SETTLED',symbol='GBPUSD',direction='CALL',
                duration_minutes=1,stake='5.00',pnl='-5.00')
            sess.add(second);sess.flush()
            sess.add(db.AutoPilotAttribution(order_id=second.id,user_id=uid,
                strategy='pulso_v2',direction='CALL',reason='fresh signal 2'))
            sess.commit()
        with factory() as sess:
            user=sess.get(db.User,uid)
            orders=sess.scalars(select(db.DemoOrder).order_by(db.DemoOrder.id)).all()
            entry,last=main.v2_symbol_recovery(sess,user,orders,
                sess.get(db.RobotPlan,uid),
                sess.get(db.RobotCycleParams,uid),
                sess.get(db.PilotValidationBatch,uid))
            assert (entry.cycle,entry.stake,entry.retry)==(2,Decimal('12.50'),False)
            assert entry.symbol is None and last.symbol=='GBPUSD'
            win=db.DemoOrder(user_id=uid,request_key='cycle-cross-003',
                state='SETTLED',symbol='1HZ50V',direction='PUT',
                duration_minutes=3,stake='12.50',pnl='11.00')
            sess.add(win);sess.flush()
            sess.add(db.AutoPilotAttribution(order_id=win.id,user_id=uid,
                strategy='pulso_v2',direction='PUT',reason='fresh signal 3'))
            sess.commit()
        with factory() as sess:
            user=sess.get(db.User,uid)
            orders=sess.scalars(select(db.DemoOrder).order_by(db.DemoOrder.id)).all()
            entry,last=main.v2_symbol_recovery(sess,user,orders,
                sess.get(db.RobotPlan,uid),
                sess.get(db.RobotCycleParams,uid),
                sess.get(db.PilotValidationBatch,uid))
            assert (entry.cycle,entry.stake,entry.retry)==(0,Decimal('2.00'),False)
    finally:cleanup(engine)


def test_cycle_only_blocks_until_all_official_orders_settle(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='0.00')
    try:
        with factory() as sess:
            sess.add(db.PilotValidationBatch(user_id=uid,after_order_id=0))
            sess.add(db.RobotRun(user_id=uid,after_order_id=0))
            sess.add(db.RobotPlan(user_id=uid,requested_martingale=False,
                gale_count=0,cycles_limit=2))
            sess.add(db.RobotCycleParams(user_id=uid,multiplier='2.50'))
            order=db.DemoOrder(user_id=uid,request_key='cycle-confirmed-001',
                state='CONFIRMED',symbol='EURUSD',direction='PUT',
                duration_minutes=5,stake='2.00')
            sess.add(order);sess.flush()
            sess.add(db.AutoPilotAttribution(order_id=order.id,user_id=uid,
                strategy='pulso_v2',direction='PUT',reason='broker still open'))
            sess.commit()
        with factory() as sess:
            user=sess.get(db.User,uid)
            rows=sess.scalars(select(db.DemoOrder)).all()
            with pytest.raises(main.ProgressionHalt,match='não liquidado'):
                main.v2_symbol_recovery(sess,user,rows,
                    sess.get(db.RobotPlan,uid),
                    sess.get(db.RobotCycleParams,uid),
                    sess.get(db.PilotValidationBatch,uid))
    finally:cleanup(engine)


@pytest.mark.parametrize('minutes',[1,3,5,15])
def test_martingale_dispatch_only_at_next_matching_candle(minutes):
    settlement=datetime(2026,11,16,12,0,1,tzinfo=timezone.utc)
    period=minutes*60
    opening=(int(settlement.timestamp()//period)+1)*period
    target,ready=main.martingale_next_candle_window(
        settlement,minutes,now=opening-0.1)
    assert target==opening and not ready
    target,ready=main.martingale_next_candle_window(
        settlement,minutes,now=opening+2)
    assert target==opening and ready
    target,ready=main.martingale_next_candle_window(
        settlement,minutes,now=opening+10)
    assert target==opening+period and not ready

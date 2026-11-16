"""Safety checks for the continuous official Deriv DEMO AutoPilot."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import main,db
from app.security import encrypt,hashsecret
from app.demo_ledger import settle


def prepare(tmp_path,monkeypatch,*,mode='demo',stop_loss='10.00'):
    engine=create_engine(f'sqlite:///{tmp_path}/pilot.sqlite')
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,'SessionLocal',factory)
    monkeypatch.setenv('NEXORA_DEMO_BUY_ENABLED','true')
    monkeypatch.setenv('NEXORA_AUTOPILOT_DEMO_ENABLED','true')
    main.autopilot_enabled.clear()
    main.autopilot_last_attempt.clear()
    main.autopilot_reasons.clear()
    main.runtimes.clear()
    with factory() as s:
        user=db.User(deriv_account_id='V123' if mode=='demo' else 'R123',selected_mode=mode,
                     stake='2.00',stop_loss=stop_loss,stop_win='20.00')
        s.add(user);s.flush()
        uid=user.id
        s.add(db.Session(session_hash=hashsecret('pilot-sid'),user_id=uid,
                         token_encrypted=encrypt('token-dummy'),
                         expires_at=db.utcnow()+timedelta(hours=1)))
        s.commit()
    client=TestClient(main.app)
    client.cookies.set('nexora_sid','pilot-sid')
    return client,factory,engine,uid


def cleanup(engine):
    main.autopilot_enabled.clear()
    main.autopilot_last_attempt.clear()
    main.autopilot_reasons.clear()
    main.runtimes.clear()
    engine.dispose()


def test_start_requires_explicit_consent_and_feature_flag(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    try:
        denied=client.post('/api/autopilot/start',json={'confirm':'wrong'})
        assert denied.status_code==403
        monkeypatch.setenv('NEXORA_AUTOPILOT_DEMO_ENABLED','false')
        disabled=client.post('/api/autopilot/start',json={'confirm':'START_DEMO_AUTOPILOT'})
        assert disabled.status_code==503
        assert uid not in main.autopilot_enabled
        assert client.get('/api/status').json()['auto_buy_enabled'] is False
    finally: cleanup(engine)


def test_real_account_cannot_start(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,mode='real')
    try:
        resp=client.post('/api/autopilot/start',json={'confirm':'START_DEMO_AUTOPILOT'})
        assert resp.status_code==403
        assert uid not in main.autopilot_enabled
    finally: cleanup(engine)


def test_continuous_demo_orders_one_at_a_time_and_attributed(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    broker=AsyncMock(side_effect=[
        {'contract_id':'555001','buy_price':'2.00'},
        {'contract_id':'555002','buy_price':'2.00'}
    ])
    monkeypatch.setattr(main,'demo_buy',broker)
    monkeypatch.setattr(main,'market_connected',True)
    monkeypatch.setattr(main,'recent_prices',[100+i*.05 for i in range(100)])
    try:
        assert client.post('/api/autopilot/start',json={'confirm':'START_DEMO_AUTOPILOT'}).status_code==200
        assert client.get('/api/status').json()['running'] is True
        async def execute():
            main.market_last_tick=asyncio.get_running_loop().time()
            await main.autopilot_once()
            await main.autopilot_once()   # no second buy during active contract
        asyncio.run(execute())
        assert broker.await_count==1
        with factory() as sess:
            orders=sess.scalars(select(db.DemoOrder)).all()
            assert len(orders)==1 and orders[0].state=='CONFIRMED'
            attribution=sess.scalar(select(db.AutoPilotAttribution))
            assert attribution.order_id==orders[0].id
            assert attribution.strategy=='tendencia'
            settle(sess,orders[0],{'contract_id':'555001','is_sold':True,'profit':'-2.00','status':'lost'})
        # No fixed trade-count cap: a second buy can happen once the first officially settles.
        main.autopilot_last_attempt[uid]=-1e12
        async def again():
            main.market_last_tick=asyncio.get_running_loop().time()
            await main.autopilot_once()
        asyncio.run(again())
        assert broker.await_count==2
        with factory() as sess:
            assert len(sess.scalars(select(db.DemoOrder)).all())==2
            assert len(sess.scalars(select(db.AutoPilotAttribution)).all())==2
        assert client.post('/api/autopilot/stop').status_code==200
        assert uid not in main.autopilot_enabled
        asyncio.run(again())
        assert broker.await_count==2
    finally: cleanup(engine)



def test_confirmed_contract_keeps_autopilot_armed_without_duplicate_buy(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='0.00')
    broker=AsyncMock(side_effect=[{'contract_id':'880001','buy_price':'2.00'},
                                  {'contract_id':'880002','buy_price':'2.00'}])
    monkeypatch.setattr(main,'demo_buy',broker)
    monkeypatch.setattr(main,'market_connected',True)
    monkeypatch.setattr(main,'recent_prices',[100+i*.05 for i in range(100)])
    try:
        assert client.post('/api/autopilot/start',
            json={'confirm':'START_DEMO_AUTOPILOT'}).status_code==200
        async def attempt():
            main.market_last_tick=asyncio.get_running_loop().time()
            await main.autopilot_once()
        asyncio.run(attempt())
        assert broker.await_count==1
        # Explicitly bypass rate limiting to exercise the progression guard.
        main.autopilot_last_attempt[uid]=-1e12
        asyncio.run(attempt())
        assert uid in main.autopilot_enabled
        assert broker.await_count==1
        status=client.get('/api/status').json()
        assert status['running'] is True
        assert 'Aguardando liquidação oficial' in status['reason']
        with factory() as sess:
            order=sess.scalar(select(db.DemoOrder))
            assert order.state=='CONFIRMED'
            settle(sess,order,{'contract_id':'880001',
                'is_sold':True,'profit':'-2.00','status':'lost'})
        main.autopilot_last_attempt[uid]=-1e12
        asyncio.run(attempt())
        assert broker.await_count==2
    finally:
        cleanup(engine)


def test_stop_loss_ends_automatic_session(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='2.00')
    broker=AsyncMock(side_effect=AssertionError('broker must not receive buy'))
    monkeypatch.setattr(main,'demo_buy',broker)
    monkeypatch.setattr(main,'market_connected',True)
    monkeypatch.setattr(main,'recent_prices',[100+i*.05 for i in range(100)])
    try:
        with factory() as sess:
            o=db.DemoOrder(user_id=uid,request_key='already-settled',active_slot=None,
                           state='SETTLED',symbol='1HZ100V',direction='PUT',
                           duration_minutes=5,stake='2.00',pnl='-2.00')
            sess.add(o);sess.commit()
        resp=client.post('/api/autopilot/start',json={'confirm':'START_DEMO_AUTOPILOT'})
        assert resp.status_code==409
        assert uid not in main.autopilot_enabled
        broker.assert_not_awaited()
    finally: cleanup(engine)


def test_logout_disables_auto_trading(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    try:
        assert client.post('/api/autopilot/start',json={'confirm':'START_DEMO_AUTOPILOT'}).status_code==200
        assert client.post('/api/logout').status_code==200
        assert uid not in main.autopilot_enabled
    finally: cleanup(engine)


def test_strategy_stats_uses_only_verified_settled_contracts(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    try:
        with factory() as sess:
            a=db.DemoOrder(user_id=uid,request_key='abc123456',state='SETTLED',
                symbol='1HZ100V',direction='CALL',duration_minutes=5,stake='2.00',
                pnl='1.76')
            b=db.DemoOrder(user_id=uid,request_key='abc123457',state='SETTLED',
                symbol='1HZ100V',direction='PUT',duration_minutes=5,stake='2.00',
                pnl='-2.00')
            c=db.DemoOrder(user_id=uid,request_key='abc123458',state='CONFIRMED',
                active_slot=uid,symbol='1HZ100V',direction='CALL',duration_minutes=5,stake='2.00',
                contract_id='newcontract')
            sess.add_all([a,b,c]);sess.flush()
            for order,strategy in [(a,'tendencia'),(b,'tendencia'),(c,'rompimento')]:
                sess.add(db.AutoPilotAttribution(order_id=order.id,user_id=uid,
                    strategy=strategy,direction=order.direction,reason='teste'))
            sess.commit()
        response=client.get('/api/autopilot/strategy-stats')
        assert response.status_code==200
        stats={row['strategy']:row for row in response.json()['items']}
        assert stats['tendencia']['settled']==2
        assert stats['tendencia']['wins']==1
        assert stats['tendencia']['losses']==1
        assert stats['tendencia']['win_rate']==0.5
        assert stats['tendencia']['net_pnl']=='-0.24'
        assert stats['rompimento']['pending']==1
        assert stats['rompimento']['settled']==0
        assert stats['rompimento']['win_rate'] is None
    finally: cleanup(engine)


def test_reconcile_unknown_with_official_id_but_never_without_id(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    broker=AsyncMock(return_value={'contract':{
        'contract_id':'77001','is_sold':True,'profit':'1.50','status':'won'}})
    monkeypatch.setattr(main,'demo_contract_status',broker)
    try:
        with factory() as s:
            s.add_all([
                db.DemoOrder(user_id=uid,request_key='known-unknown',
                    active_slot=uid,state='UNKNOWN',contract_id='77001',
                    symbol='1HZ100V',direction='CALL',duration_minutes=5,stake='2.00',
                    created_at=db.utcnow()-timedelta(minutes=6)),
                db.DemoOrder(user_id=uid,request_key='unknown-without-id',
                    active_slot=None,state='UNKNOWN',contract_id=None,
                    symbol='1HZ100V',direction='PUT',duration_minutes=5,stake='2.00')
            ])
            s.commit()
        asyncio.run(main.reconcile_confirmed_orders_once())
        assert broker.await_count==1
        with factory() as s:
            known=s.scalar(select(db.DemoOrder).where(db.DemoOrder.request_key=='known-unknown'))
            unresolved=s.scalar(select(db.DemoOrder).where(db.DemoOrder.request_key=='unknown-without-id'))
            assert known.state=='SETTLED'
            assert known.pnl=='1.50'
            assert known.active_slot is None
            assert unresolved.state=='UNKNOWN'
            assert unresolved.active_slot is None
            assert unresolved.pnl is None
    finally:
        cleanup(engine)


def test_reconciliation_prioritizes_recent_contracts(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    async def open_contract(token,account_id,contract_id):
        return {'contract':{'contract_id':contract_id,'is_sold':False,'status':'open'}}
    broker=AsyncMock(side_effect=open_contract)
    monkeypatch.setattr(main,'demo_contract_status',broker)
    try:
        with factory() as s:
            s.add_all([
                db.DemoOrder(user_id=uid,request_key=f'pending-{i:03d}',
                    active_slot=None,state='CONFIRMED',contract_id=str(100000+i),
                    symbol='1HZ100V',direction='CALL',duration_minutes=5,stake='2.00',
                    created_at=db.utcnow()-timedelta(minutes=6))
                for i in range(105)
            ])
            s.commit()
        asyncio.run(main.reconcile_confirmed_orders_once())
        assert broker.await_count==100
        contract_ids=[call.args[2] for call in broker.await_args_list]
        assert contract_ids[0]=='100104'
        assert contract_ids[-1]=='100005'
        assert '100000' not in contract_ids
    finally:
        cleanup(engine)


def test_resume_consent_created_on_manual_start_and_cleared_on_stop(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    try:
        assert client.post('/api/autopilot/start',json={'confirm':'START_DEMO_AUTOPILOT'}).status_code==200
        with factory() as s:
            assert s.get(db.AutoPilotResumeConsent,uid) is not None
            original=s.get(db.RobotRun,uid).after_order_id
        main.autopilot_enabled.clear()  # simulate memory loss during restart
        main.restore_demo_autopilot_consent()
        assert uid in main.autopilot_enabled
        with factory() as s:
            assert s.get(db.RobotRun,uid).after_order_id==original
        assert client.post('/api/autopilot/stop').status_code==200
        with factory() as s:
            assert s.get(db.AutoPilotResumeConsent,uid) is None
        main.autopilot_enabled.clear()
        main.restore_demo_autopilot_consent()
        assert uid not in main.autopilot_enabled
    finally: cleanup(engine)


def test_old_robot_run_does_not_imply_resume_consent(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    try:
        with factory() as s:
            s.add(db.RobotRun(user_id=uid,after_order_id=0))
            s.commit()
        main.restore_demo_autopilot_consent()
        assert uid not in main.autopilot_enabled
    finally: cleanup(engine)


def test_resume_rejects_unknown_order_and_revokes_consent(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    try:
        assert client.post('/api/autopilot/start',json={'confirm':'START_DEMO_AUTOPILOT'}).status_code==200
        with factory() as s:
            s.add(db.DemoOrder(user_id=uid,request_key='uncertain-resume',
                state='UNKNOWN',symbol='1HZ100V',direction='CALL',
                duration_minutes=5,stake='2.00',active_slot=uid))
            s.commit()
        main.autopilot_enabled.clear()
        main.restore_demo_autopilot_consent()
        assert uid not in main.autopilot_enabled
        with factory() as s:
            assert s.get(db.AutoPilotResumeConsent,uid) is None
        assert 'pendente ou incerto' in main.autopilot_reasons[uid]
    finally: cleanup(engine)


def test_resume_rejects_expired_session_and_revokes_consent(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    try:
        assert client.post('/api/autopilot/start',json={'confirm':'START_DEMO_AUTOPILOT'}).status_code==200
        with factory() as s:
            auth=s.scalar(select(db.Session).where(db.Session.user_id==uid))
            auth.expires_at=db.utcnow()-timedelta(minutes=2)
            s.commit()
        main.autopilot_enabled.clear()
        main.restore_demo_autopilot_consent()
        assert uid not in main.autopilot_enabled
        with factory() as s:
            assert s.get(db.AutoPilotResumeConsent,uid) is None
    finally: cleanup(engine)


def test_stop_loss_halt_revokes_resume_consent(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='2.00')
    try:
        assert client.post('/api/autopilot/start',json={'confirm':'START_DEMO_AUTOPILOT'}).status_code==200
        with factory() as s:
            s.add(db.DemoOrder(user_id=uid,request_key='loss-after-start',
                state='SETTLED',symbol='1HZ100V',direction='CALL',
                duration_minutes=5,stake='2.00',pnl='-2.00'))
            s.commit()
        monkeypatch.setattr(main,'market_connected',True)
        monkeypatch.setattr(main,'recent_prices',[100+i*.05 for i in range(100)])
        async def tick():
            main.market_last_tick=asyncio.get_running_loop().time()
            await main.autopilot_once()
        asyncio.run(tick())
        assert uid not in main.autopilot_enabled
        with factory() as s:
            assert s.get(db.AutoPilotResumeConsent,uid) is None
    finally: cleanup(engine)


def test_resume_with_confirmed_contract_keeps_broker_buy_blocked(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    try:
        assert client.post('/api/autopilot/start',json={'confirm':'START_DEMO_AUTOPILOT'}).status_code==200
        with factory() as s:
            order=db.DemoOrder(user_id=uid,request_key='known-contract',
                state='CONFIRMED',symbol='1HZ100V',direction='CALL',contract_id='1234567',
                active_slot=uid,duration_minutes=5,stake='2.00')
            s.add(order)
            s.flush()
            s.add(db.AutoPilotAttribution(order_id=order.id,user_id=uid,
                strategy='tendencia',direction='CALL',reason='historico oficial'))
            s.commit()
        main.autopilot_enabled.clear()
        main.restore_demo_autopilot_consent()
        # A confirmed contract may remain armed only while the normal loop
        # waits for broker settlement and never buys a second one.
        broker=AsyncMock(side_effect=AssertionError('duplicate broker buy'))
        monkeypatch.setattr(main,'demo_buy',broker)
        monkeypatch.setattr(main,'market_connected',True)
        monkeypatch.setattr(main,'recent_prices',[100+i*.05 for i in range(100)])
        async def tick():
            main.market_last_tick=asyncio.get_running_loop().time()
            await main.autopilot_once()
        asyncio.run(tick())
        broker.assert_not_awaited()
        assert uid in main.autopilot_enabled
    finally: cleanup(engine)

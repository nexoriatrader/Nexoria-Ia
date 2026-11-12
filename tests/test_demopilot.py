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

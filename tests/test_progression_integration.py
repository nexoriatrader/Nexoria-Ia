"""Integration tests for real broker-confirmed DEMO progression and restart handling."""
import asyncio
from datetime import datetime,timedelta,timezone
from unittest.mock import AsyncMock
from sqlalchemy import select,update
from app import main,db
from app.demo_ledger import settle
from test_demopilot import prepare,cleanup


def test_end_to_end_demo_progression_executes_2_4_8_16_then_stops(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='0.00')
    monkeypatch.setattr(main,'demo_buy',AsyncMock(side_effect=[
        {'contract_id':'6601','buy_price':'2.00'},
        {'contract_id':'6602','buy_price':'4.00'},
        {'contract_id':'6603','buy_price':'8.00'},
        {'contract_id':'6604','buy_price':'16.00'}]))
    monkeypatch.setattr(main,'market_connected',True)
    monkeypatch.setattr(main,'recent_prices',[100+i*0.05 for i in range(100)])
    monkeypatch.setattr(main,'m1_recovery_window_open',lambda settled_at: True)
    try:
        with factory() as sess:
            user=sess.get(db.User,uid)
            user.stop_win='0.00'
            plan=db.RobotPlan(user_id=uid,requested_martingale=True,
                gale_count=1,requested_multiplier='2.0',cycles_limit=1)
            cp=db.RobotCycleParams(user_id=uid,multiplier='2.0')
            sess.add_all([plan,cp]);sess.commit()
        assert client.post('/api/autopilot/start',json={'confirm':'START_DEMO_AUTOPILOT'}).status_code==200
        for idx,expected in enumerate(['2.00','4.00','8.00','16.00'],1):
            main.autopilot_last_attempt[uid]=-1e12
            async def run():
                main.market_last_tick=asyncio.get_running_loop().time()
                await main.autopilot_once()
            asyncio.run(run())
            with factory() as sess:
                orders=sess.scalars(select(db.DemoOrder).order_by(db.DemoOrder.id)).all()
                assert len(orders)==idx
                order=orders[-1]
                assert order.stake==expected
                assert order.state=='CONFIRMED'
                settle(sess,order,{'contract_id':str(6600+idx),
                     'is_sold':True,'profit':'-'+expected,'status':'lost'})
                sess.execute(update(db.DemoOrder).where(db.DemoOrder.id==order.id)
                    .values(updated_at=datetime.now(timezone.utc)-timedelta(minutes=3)))
                sess.commit()
        main.autopilot_last_attempt[uid]=-1e12
        asyncio.run(run())
        assert main.demo_buy.await_count==4
        assert uid not in main.autopilot_enabled
        assert 'ciclos concluída' in main.autopilot_reasons[uid]
    finally:cleanup(engine)


def test_restart_is_detected_not_automatically_rearmed(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    try:
        assert client.post('/api/autopilot/start',json={'confirm':'START_DEMO_AUTOPILOT'}).status_code==200
        assert uid in main.autopilot_enabled
        main.autopilot_enabled.clear()  # New worker after a Render deploy.
        main.autopilot_reasons.clear()
        status=client.get('/api/status')
        assert status.status_code==200
        assert status.json()['running'] is False
        assert status.json()['interrupted_after_restart'] is True
        assert 'reative manualmente' in status.json()['reason']
        assert client.post('/api/autopilot/stop').status_code==200
        assert client.get('/api/status').json()['interrupted_after_restart'] is False
    finally:cleanup(engine)


def test_real_account_classification_is_strict_and_read_only():
    from app.auth_helpers import real_accounts
    accounts=[
      {'account_type':'real','account_id':'R123','currency':'USD','status':'active'},
      {'account_type':'demo','account_id':'V123'},
      {'account_type':'real','account_id':'VFAKE','is_virtual':True},
      {'account_id':'RUNCERTAIN'}]
    assert real_accounts(accounts)==[{'account_id':'R123','account_type':'real',
                                       'currency':'USD','status':'active'}]

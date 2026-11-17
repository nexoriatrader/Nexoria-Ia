"""Integration tests for real broker-confirmed DEMO progression and restart handling."""
import asyncio
from datetime import datetime,timedelta,timezone
from unittest.mock import AsyncMock
from sqlalchemy import select,update
from app import main,db
from app.demo_ledger import settle
from test_demopilot import prepare,cleanup


def test_combined_martingale_and_cycles_never_authorizes_demo_buys(tmp_path,monkeypatch):
    """Martingale and cycle progressions are mutually exclusive by design."""
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='0.00')
    buy=AsyncMock(side_effect=AssertionError('No broker orders for conflicting settings'))
    monkeypatch.setattr(main,'demo_buy',buy)
    try:
        with factory() as sess:
            sess.add(db.RobotPlan(user_id=uid,requested_martingale=True,
                gale_count=1,requested_multiplier='2.0',cycles_limit=1))
            sess.add(db.RobotCycleParams(user_id=uid,multiplier='2.0'))
            sess.commit()
        response=client.post('/api/autopilot/start',json={'confirm':'START_DEMO_AUTOPILOT'})
        assert response.status_code==409,response.text
        assert 'Martingale OU ciclos' in response.json()['detail']
        assert uid not in main.autopilot_enabled
        with factory() as sess:
            assert sess.query(db.DemoOrder).count()==0
        buy.assert_not_awaited()
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

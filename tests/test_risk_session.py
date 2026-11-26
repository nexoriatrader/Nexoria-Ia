"""Risk-session reset preserves official ledger and requires an idle, authenticated bot."""
from app import db,main
from test_demopilot import prepare,cleanup


def _loss(session,uid,key):
    session.add(db.DemoOrder(user_id=uid,request_key=key,
        state='SETTLED',active_slot=None,symbol='1HZ100V',
        direction='CALL',duration_minutes=5,stake='2.00',pnl='-2.00',source_panel_mode='strategies'))
    session.commit()


def test_reset_keeps_history_and_reopens_session_stop(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='2.00')
    try:
        with factory() as s:
            _loss(s,uid,'loss-previous')
        assert client.get('/api/autopilot/readiness?panel_mode=strategies').json()['ready'] is False
        assert client.post('/api/demo/risk-session/reset',json={'confirm':'wrong','panel_mode':'strategies'}).status_code==403
        reset=client.post('/api/demo/risk-session/reset',
            json={'confirm':'RESET_DEMO_RISK_SESSION','panel_mode':'strategies'})
        assert reset.status_code==200,reset.text
        info=client.get('/api/demo/risk-session?panel_mode=strategies').json()
        assert info['pnl']=='0.00'
        assert info['after_order_id']==1
        assert client.get('/api/autopilot/readiness?panel_mode=strategies').json()['ready'] is True
        assert client.get('/api/demo/results/today').status_code==200
        with factory() as s:
            assert len(s.query(db.DemoOrder).all())==1
            assert s.query(db.DemoOrder).first().pnl=='-2.00'
            assert len(s.query(db.RiskSession).all())==1
            _loss(s,uid,'loss-new-session')
        assert client.get('/api/demo/risk-session?panel_mode=strategies').json()['pnl']=='-2.00'
        assert client.get('/api/autopilot/readiness?panel_mode=strategies').json()['ready'] is False
    finally:
        cleanup(engine)


def test_reset_refuses_open_contract_and_active_robot(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='2.00')
    try:
        with factory() as s:
            s.add(db.DemoOrder(user_id=uid,request_key='waiting',
                state='UNKNOWN',active_slot=uid,symbol='1HZ100V',
                direction='PUT',duration_minutes=5,stake='2.00',source_panel_mode='strategies'))
            s.commit()
        blocked=client.post('/api/demo/risk-session/reset',
            json={'confirm':'RESET_DEMO_RISK_SESSION','panel_mode':'strategies'})
        assert blocked.status_code==409
        with factory() as s:
            assert s.query(db.RiskSession).count()==0
            pending=s.query(db.DemoOrder).first()
            pending.state='SETTLED'
            pending.pnl='-2.00'
            pending.active_slot=None
            s.commit()
        main.autopilot_enabled.add(uid)
        with factory() as s:
            s.get(db.AutomationExecutionState,uid).active_panel_mode='strategies'
            s.commit()
        blocked=client.post('/api/demo/risk-session/reset',
            json={'confirm':'RESET_DEMO_RISK_SESSION','panel_mode':'strategies'})
        assert blocked.status_code==409
        main.autopilot_enabled.discard(uid)
    finally:
        cleanup(engine)


def test_reset_requires_authentication(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    try:
        client.cookies.clear()
        assert client.get('/api/demo/risk-session?panel_mode=strategies').status_code==401
        assert client.post('/api/demo/risk-session/reset',
            json={'confirm':'RESET_DEMO_RISK_SESSION','panel_mode':'strategies'}).status_code==401
    finally:
        cleanup(engine)


def test_reset_revokes_durable_resume_intent(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    try:
        with factory() as s:
            s.add(db.RobotRun(user_id=uid,after_order_id=0))
            s.add(db.AutoPilotResumeConsent(user_id=uid))
            s.commit()
        result=client.post('/api/demo/risk-session/reset',
            json={'confirm':'RESET_DEMO_RISK_SESSION','panel_mode':'strategies'})
        assert result.status_code==200
        with factory() as s:
            assert s.get(db.RobotRun,uid) is None
            assert s.get(db.AutoPilotResumeConsent,uid) is None
        main.pause_demo_autopilot_after_restart()
        assert uid not in main.autopilot_enabled
    finally:
        cleanup(engine)

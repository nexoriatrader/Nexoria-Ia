"""Regression: the AutoPilot start guard must expose a truthful visible reason."""
from pathlib import Path
from test_demopilot import prepare,cleanup
from app import db,main


def test_readiness_exposes_stop_loss_blocker_without_starting(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='2.00')
    try:
        with factory() as session:
            session.add(db.DemoOrder(user_id=uid,request_key='older-loss',
                active_slot=None,state='SETTLED',symbol='1HZ100V',direction='CALL',
                duration_minutes=5,stake='2.00',pnl='-2.00',source_panel_mode='strategies'))
            session.commit()
        check=client.get('/api/autopilot/readiness?panel_mode=strategies')
        assert check.status_code==200
        assert check.json()['ready'] is False
        assert 'Stop Loss' in check.json()['reason']
        blocked=client.post('/api/autopilot/start',json={'confirm':'START_DEMO_AUTOPILOT','panel_mode':'strategies'})
        assert blocked.status_code==409
        assert blocked.json()['detail']==check.json()['reason']
        assert uid not in main.autopilot_enabled
    finally:
        cleanup(engine)


def test_readiness_blocks_pending_but_does_not_override_confirmed(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch,stop_loss='0')
    try:
        with factory() as session:
            order=db.DemoOrder(user_id=uid,request_key='uncertain',
                active_slot=uid,state='UNKNOWN',symbol='1HZ100V',direction='PUT',
                duration_minutes=5,stake='2.00',source_panel_mode='strategies')
            session.add(order);session.commit()
        info=client.get('/api/autopilot/readiness?panel_mode=strategies').json()
        assert info['ready'] is False
        assert 'pendente ou incerto' in info['reason']
        assert client.post('/api/autopilot/start',
            json={'confirm':'START_DEMO_AUTOPILOT','panel_mode':'strategies'}).status_code==409
    finally:cleanup(engine)


def test_dashboard_shows_persistent_stop_notice_below_start_button():
    html=(Path(__file__).resolve().parents[1]/'app'/'index.html').read_text()
    button=html.index('id="robotButton"')
    notice=html.index('id="robotStartNotice"')
    assert button < notice < html.index('id="feedState"')
    assert 'role="alert"' in html
    assert 'robot-notice' in html
    assert 'showRobotNotice(reason)' in html
    assert "/api/autopilot/readiness" in html
    assert 'const status=await refreshDashboard();' in html
    assert "NÃO apaga perdas, resultados" in html
    assert 'onclick="resetRiskSession()"' in html
    assert 'onclick="go(\'robot-config\',\'strategies\')"' in html
    # No temporary toast should be used for the start failure.
    assert "fail(reason);" not in html

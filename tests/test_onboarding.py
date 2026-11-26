"""Onboarding configuration regression: saved preferences must never authorize real orders."""
from datetime import timedelta
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import db, main
from app.security import hashsecret,encrypt


def make_client(tmp_path,monkeypatch):
    engine=create_engine(f'sqlite:///{tmp_path}/onboarding.sqlite')
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,'SessionLocal',factory)
    with factory() as sess:
        user=db.User(deriv_account_id='VONBOARD',selected_mode='demo')
        sess.add(user);sess.flush()
        sess.add(db.Session(session_hash=hashsecret('onboarding_sid'),user_id=user.id,
                            token_encrypted=encrypt('mock-demo'),
                            expires_at=db.utcnow()+timedelta(hours=1)))
        sess.commit()
        uid=user.id
    client=TestClient(main.app)
    client.cookies.set('nexora_sid','onboarding_sid')
    return client,factory,uid,engine


def test_onboarding_saves_limits_and_future_preferences_without_triggering_broker_orders(tmp_path,monkeypatch):
    client,factory,uid,engine=make_client(tmp_path,monkeypatch)
    buy=AsyncMock(side_effect=AssertionError('No broker orders during onboarding'))
    monkeypatch.setattr(main,'demo_buy',buy)
    payload={'panel_mode':'strategies','stake':'2.50','stop_win':'20','stop_loss':'12',
             'martingale':True,'gale_count':2,'multiplier':'3',
             'cycles':'5'}
    try:
        before=client.get('/api/onboarding/config?panel_mode=strategies')
        assert before.status_code==200
        assert before.json()['mode']=='demo'
        assert before.json()['stop_win']=='0.00'
        assert before.json()['stop_loss']=='0.00'
        assert before.json()['martingale_execution_enabled'] is True
        # Old onboarding allowed BOTH modes. That is now explicitly
        # prohibited, without changing saved state or sending any buy.
        response=client.post('/api/onboarding/config',json=payload)
        assert response.status_code==422,response.text
        assert 'Martingale' in response.json()['detail']
        assert client.get('/api/onboarding/config?panel_mode=strategies').json()['cycles']=='0'

        martingale_only={**payload,'cycles':'0'}
        response=client.post('/api/onboarding/config',json=martingale_only)
        assert response.status_code==200,response.text
        assert response.json()['martingale_execution_enabled'] is True
        after=client.get('/api/onboarding/config?panel_mode=strategies').json()
        assert after['martingale'] is True
        assert after['gale_count']==2
        assert after['multiplier']=='3'
        assert after['cycles']=='0'

        cycles_only={**payload,'martingale':False,'gale_count':0,'cycles':'5',
                     'cycle_multiplier':'2.50'}
        response=client.post('/api/onboarding/config',json=cycles_only)
        assert response.status_code==200,response.text
        assert response.json()['cycles_execution_enabled'] is True
        after=client.get('/api/onboarding/config?panel_mode=strategies').json()
        assert after['martingale'] is False
        assert after['gale_count']==0
        assert after['cycles']=='5'
        assert after['cycle_multiplier']=='2.50'
        assert after['stake']=='2.50'
        with factory() as sess:
            u=sess.get(db.User,uid)
            assert u.martingale is False
            assert u.multiplier=='2.00'
            assert u.stop_loss=='0.00'
            profile=sess.get(db.AutomationPanelProfile,(uid,'strategies'))
            assert profile.cycles_limit==5
            assert profile.stop_loss=='12.00'
            assert sess.get(db.RobotPlan,uid) is None
        buy.assert_not_awaited()
    finally:
        main.runtimes.pop(uid,None)
        engine.dispose()


def test_onboarding_validates_cycle_and_martingale_inputs(tmp_path,monkeypatch):
    client,factory,uid,engine=make_client(tmp_path,monkeypatch)
    valid={'panel_mode':'strategies','stake':'2','stop_win':'20','stop_loss':'10',
           'martingale':False,'gale_count':0,'multiplier':'2','cycles':'0'}
    try:
        for override in ({'cycles':'-1'}, {'cycles':'6'}, {'gale_count':6},
                         {'martingale':True,'gale_count':0},
                         {'martingale':False,'gale_count':1},
                         {'martingale':True,'gale_count':2,'cycles':'1'},
                         {'multiplier':'invalid'}, {'multiplier':'0.9'}, {'cycle_multiplier':'0'}, {'stake':'0'}):
            assert client.post('/api/onboarding/config',json={**valid,**override}).status_code==422
        assert client.get('/api/onboarding/config?panel_mode=strategies').json()['cycles']=='0'
        assert client.post('/api/onboarding/config',json=valid).status_code==200
    finally:
        main.runtimes.pop(uid,None)
        engine.dispose()


def test_onboarding_requires_session_and_never_enables_real_orders(tmp_path,monkeypatch):
    client,factory,uid,engine=make_client(tmp_path,monkeypatch)
    try:
        client.cookies.clear()
        assert client.get('/api/onboarding/config?panel_mode=strategies').status_code==401
        assert client.post('/api/onboarding/config',json={}).status_code==401
    finally:
        main.runtimes.pop(uid,None)
        engine.dispose()


def test_configuration_is_panel_scoped_and_wizard_is_reached_from_each_panel():
    from pathlib import Path
    html=(Path(__file__).resolve().parents[1]/'app'/'index.html').read_text()
    nav=html.split('<nav id="primaryNavigation"',1)[1].split('</nav>',1)[0]
    assert 'Nexora Manual' in nav and 'Nexora Estratégias' in nav
    assert 'data-view="app-home"' in nav
    assert "go('app-home')" in nav
    assert 'data-view="robot-config"' not in nav
    assert "go('robot-config','manual')" in html
    assert "go('robot-config','strategies')" in html
    assert "req('/api/automation/profile?panel_mode='+encodeURIComponent(panelMode))" in html
    assert 'Nexora Manual · configuração completa' in html


def test_account_page_identity_referrals_and_plan_catalog_are_separate():
    from pathlib import Path
    html=(Path(__file__).resolve().parents[1]/'app'/'index.html').read_text()
    account=html.split('<section id="account"',1)[1].split('<p id="error"',1)[0]
    assert 'id="accountUserId"' in account
    assert 'CORRETORA CONECTADA' in account.upper()
    assert 'onclick="logout()"' in account
    assert 'Deriv Demo <span class="pill">' not in account
    assert 'id="accountBalance"' not in account
    assert account.index('id="accountUserId"') < account.index('id="robotAccessCountdown"')
    assert account.index('id="referralSummary"') < account.index('id="accountPlansHeading"')
    assert 'R$</small> 29,90' in account
    assert 'R$</small> 49,90' in account
    assert 'R$</small> 99,00' in account
    assert 'id="accountBillingNote"' in account
    assert 'data-plan="quinzenal"' in account
    assert 'data-plan="mensal"' in account
    assert 'data-plan="trimestral"' in account
    assert 'disabled data-plan=' in account
    assert "accountUserId').textContent=String(a.user_id||'—')" in html
    assert "if(v==='account'){loadRobotAccess();loadBillingPlans()}" in html


def test_robot_wizard_has_exactly_seven_main_steps_and_cycles_stay_in_step_six():
    from pathlib import Path
    html=(Path(__file__).resolve().parents[1]/'app'/'index.html').read_text()
    config=html.split('<section id="robot-config"',1)[1].split('<section id="results"',1)[0]
    steps=config.split('<div class="auto-steps">',1)[1].split('<div class="auto-wizard-actions">',1)[0]
    assert steps.count('<div class="auto-step">')==7
    assert 'class="auto-cycle-subheading"' in steps
    assert '<span>06B</span>' in steps
    assert steps.index('<span>06B</span>')<steps.index('<span>07</span>')
    assert "if(headers.length!==7)throw Error('Etapas de configuração incompletas')" in html
    assert "element.hidden=Number(element.dataset.wizardStep)!==autoWizardStep" in html
    assert 'id="autoWizardBack"' in config
    assert 'id="autoWizardNext"' in config
    assert 'id="autoSaveButton"' in config


def test_results_screen_has_no_pulso_audit_button_or_panel():
    from pathlib import Path
    html=(Path(__file__).resolve().parents[1]/'app'/'index.html').read_text()
    results=html.split('<section id="results"',1)[1].split('<section id="settings"',1)[0]
    assert 'AUDITORIA PULSO' not in results
    assert 'pulsoAuditCard' not in results
    assert 'loadPulsoAudit' not in html
    assert 'id="ordersBody"' in results
    assert 'id="resultsLiveStatus"' in results

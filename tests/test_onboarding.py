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
    payload={'stake':'2.50','stop_win':'20','stop_loss':'12',
             'martingale':True,'gale_count':2,'multiplier':'3',
             'cycles':'5'}
    try:
        before=client.get('/api/onboarding/config')
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
        assert client.get('/api/onboarding/config').json()['cycles']=='0'

        martingale_only={**payload,'cycles':'0'}
        response=client.post('/api/onboarding/config',json=martingale_only)
        assert response.status_code==200,response.text
        assert response.json()['martingale_execution_enabled'] is True
        after=client.get('/api/onboarding/config').json()
        assert after['martingale'] is True
        assert after['gale_count']==2
        assert after['multiplier']=='3'
        assert after['cycles']=='0'

        cycles_only={**payload,'martingale':False,'gale_count':0,'cycles':'5',
                     'cycle_multiplier':'2.50'}
        response=client.post('/api/onboarding/config',json=cycles_only)
        assert response.status_code==200,response.text
        assert response.json()['cycles_execution_enabled'] is True
        after=client.get('/api/onboarding/config').json()
        assert after['martingale'] is False
        assert after['gale_count']==0
        assert after['cycles']=='5'
        assert after['cycle_multiplier']=='2.50'
        assert after['stake']=='2.50'
        with factory() as sess:
            u=sess.get(db.User,uid)
            assert u.martingale is False
            assert u.multiplier=='2.00'
            assert u.stop_loss=='12.00'
            plan=sess.get(db.RobotPlan,uid)
            assert plan.cycles_limit==5
        buy.assert_not_awaited()
    finally:
        main.runtimes.pop(uid,None)
        engine.dispose()


def test_onboarding_validates_cycle_and_martingale_inputs(tmp_path,monkeypatch):
    client,factory,uid,engine=make_client(tmp_path,monkeypatch)
    valid={'stake':'2','stop_win':'20','stop_loss':'10',
           'martingale':False,'gale_count':0,'multiplier':'2','cycles':'0'}
    try:
        for override in ({'cycles':'-1'}, {'cycles':'6'}, {'gale_count':6},
                         {'martingale':True,'gale_count':0},
                         {'martingale':False,'gale_count':1},
                         {'martingale':True,'gale_count':2,'cycles':'1'},
                         {'multiplier':'invalid'}, {'multiplier':'0.9'}, {'cycle_multiplier':'0'}, {'stake':'0'}):
            assert client.post('/api/onboarding/config',json={**valid,**override}).status_code==422
        assert client.get('/api/onboarding/config').json()['cycles']=='0'
        assert client.post('/api/onboarding/config',json=valid).status_code==200
    finally:
        main.runtimes.pop(uid,None)
        engine.dispose()


def test_onboarding_requires_session_and_never_enables_real_orders(tmp_path,monkeypatch):
    client,factory,uid,engine=make_client(tmp_path,monkeypatch)
    try:
        client.cookies.clear()
        assert client.get('/api/onboarding/config').status_code==401
        assert client.post('/api/onboarding/config',json={}).status_code==401
    finally:
        main.runtimes.pop(uid,None)
        engine.dispose()


def test_legacy_setup_is_not_in_oauth_flow_and_wizard_is_used():
    from pathlib import Path
    html=(Path(__file__).resolve().parents[1]/'app'/'index.html').read_text()
    setup=html.split('<section id="setup"',1)[1].split('<section id="dashboard"',1)[0]
    assert "go('setup')" not in html\n    assert "go('robot-config');" in html\n    assert 'id="autoWizardProgress"' in html
    assert "go('dashboard')" in html
    assert 'É hora de ' in setup
    assert 'configurar seu robô' in setup
    assert 'FINALIZAR CONFIGURAÇÃO E IR PARA O PAINEL' in setup
    assert 'id="setupCycles"' in setup
    assert 'id="setupMg"' in setup
    assert 'id="setupGales"' in setup
    assert 'id="setupMultiplier"' in setup
    assert 'disabled' not in setup.split('id="setupStake"')[1].split('>',1)[0]
    assert '/api/autopilot/start' not in setup
    assert '/api/demo/buy' not in setup
    assert 'id="setupCycleMultiplier"' in setup
    assert 'A cada perda confirmada' in setup


def test_account_page_identity_referrals_and_plan_catalog_are_separate():
    from pathlib import Path
    html=(Path(__file__).resolve().parents[1]/'app'/'index.html').read_text()
    account=html.split('<section id="account"',1)[1].split('<p id="error"',1)[0]
    assert 'id="accountUserId"' in account
    assert 'Corretora conectada' in account
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
    results=html.split('<section id="results"',1)[1].split('<section id="features"',1)[0]
    assert 'AUDITORIA PULSO' not in results
    assert 'pulsoAuditCard' not in results
    assert 'loadPulsoAudit' not in html
    assert 'id="ordersBody"' in results
    assert 'id="resultsLiveStatus"' in results

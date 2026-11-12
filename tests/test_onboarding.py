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


def test_onboarding_saves_limits_and_future_preferences_without_enabling_progression(tmp_path,monkeypatch):
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
        assert before.json()['martingale_execution_enabled'] is False
        response=client.post('/api/onboarding/config',json=payload)
        assert response.status_code==200,response.text
        assert response.json()['martingale_execution_enabled'] is False
        assert response.json()['cycles_execution_enabled'] is False
        after=client.get('/api/onboarding/config').json()
        assert after['martingale'] is True
        assert after['gale_count']==2
        assert after['multiplier']=='3.00'
        assert after['cycles']=='5'
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
           'martingale':False,'gale_count':0,'multiplier':'2','cycles':'unlimited'}
    try:
        for override in ({'cycles':'-1'}, {'gale_count':4},
                         {'martingale':True,'gale_count':0},
                         {'martingale':False,'gale_count':1},
                         {'multiplier':'99'}, {'stake':'0'}):
            assert client.post('/api/onboarding/config',json={**valid,**override}).status_code==422
        assert client.get('/api/onboarding/config').json()['cycles']=='unlimited'
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


def test_third_page_is_part_of_explicit_login_flow():
    from pathlib import Path
    html=(Path(__file__).resolve().parents[1]/'app'/'index.html').read_text()
    setup=html.split('<section id="setup"',1)[1].split('<section id="dashboard"',1)[0]
    assert "go('setup')" in html
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
    assert 'martingale_execution_enabled' in html or 'Martingale ainda não executa' in html

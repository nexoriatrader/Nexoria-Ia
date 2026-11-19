"""Read-only real account identity tests with synthetic OAuth data only.

No broker WebSocket, no purchase calls, no client credentials, no live money.
"""
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db,main
from app.auth_helpers import real_balance
from app.security import encrypt,hashsecret


ACCOUNTS=[
    {'account_type':'demo','account_id':'V111','balance':'1000.00','currency':'USD'},
    {'account_type':'real','account_id':'R111','balance':'11.50','currency':'USD'},
    {'account_type':'real','account_id':'R222','balance':'22.75','currency':'EUR'},
]


class FakeResponse:
    def raise_for_status(self):
        pass
    def json(self):
        return {'data':ACCOUNTS}


class FakeAccountsClient:
    def __init__(self,**kwargs):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self,*args):
        return False
    async def get(self,url,**kwargs):
        assert url=='https://api.derivws.com/trading/v1/options/accounts'
        assert kwargs['headers']['Authorization']=='Bearer synthetic-token'
        return FakeResponse()


@pytest.fixture
def session_client(tmp_path,monkeypatch):
    engine=create_engine(f"sqlite:///{tmp_path}/read-identity.sqlite")
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,'SessionLocal',factory)
    monkeypatch.setattr(main.httpx,'AsyncClient',FakeAccountsClient)
    with factory() as session:
        user=db.User(deriv_account_id='V111',selected_mode='demo')
        session.add(user)
        session.flush()
        session.add(db.Session(
            session_hash=hashsecret('synthetic-session'),user_id=user.id,
            token_encrypted=encrypt('synthetic-token'),
            expires_at=db.utcnow()+timedelta(hours=1)))
        profile=db.AutomationProfile(
            user_id=user.id,account_mode='real',real_account_id='R222')
        session.add(profile)
        session.commit()
    client=TestClient(main.app)
    client.cookies.set('nexora_sid','synthetic-session')
    try:
        yield client,factory
    finally:
        client.close()
        engine.dispose()


def set_selection(factory,selected,mode='real'):
    with factory() as session:
        profile=session.query(db.AutomationProfile).one()
        profile.account_mode=mode
        profile.real_account_id=selected
        session.commit()


def test_real_balance_never_guesses_first_real_account():
    assert real_balance(ACCOUNTS) is None
    assert real_balance(ACCOUNTS,'R111')['balance']=='11.50'
    assert real_balance(ACCOUNTS,'R222')['balance']=='22.75'
    assert real_balance(ACCOUNTS,'V111') is None
    assert real_balance(ACCOUNTS,'R999') is None
    assert real_balance(ACCOUNTS[:2])['account_id']=='R111'


def test_selected_real_account_balance_matches_authorized_id(session_client,monkeypatch):
    client,_=session_client
    # Both balances supplied in synthetic OAuth; opening a WebSocket is forbidden.
    from app import real_readonly
    unreachable=AsyncMock(side_effect=AssertionError('unexpected broker request'))
    monkeypatch.setattr(real_readonly,'fetch_real_balance',unreachable)
    response=client.get('/api/real/balance')
    assert response.status_code==200
    result=response.json()
    assert result['available'] is True
    assert result['account_id']=='R222'
    assert result['balance']=='22.75'
    assert result['currency']=='EUR'
    unreachable.assert_not_awaited()


def test_readonly_daily_profit_uses_selected_real_account(session_client,monkeypatch):
    client,_=session_client
    from app import real_readonly
    fake=AsyncMock(return_value={
        'available':True,'pnl':'3.50','currency':'EUR',
        'account_id':'R222','source':'DERIV_REAL_PROFIT_TABLE','reason':None,
    })
    monkeypatch.setattr(real_readonly,'fetch_real_daily_profit',fake)
    response=client.get('/api/real/results/today')
    assert response.status_code==200
    assert response.json()['account_id']=='R222'
    assert response.json()['pnl']=='3.50'
    args=fake.await_args.args
    assert args[0]=='synthetic-token'
    assert args[1]=='R222'


@pytest.mark.parametrize('path',('/api/real/balance','/api/real/results/today'))
def test_ambiguous_multiple_real_accounts_fail_closed(session_client,monkeypatch,path):
    client,factory=session_client
    set_selection(factory,None,mode='demo')
    from app import real_readonly
    read_balance=AsyncMock(side_effect=AssertionError('wrong account opened'))
    read_profit=AsyncMock(side_effect=AssertionError('wrong account opened'))
    monkeypatch.setattr(real_readonly,'fetch_real_balance',read_balance)
    monkeypatch.setattr(real_readonly,'fetch_real_daily_profit',read_profit)
    response=client.get(path)
    assert response.status_code==200
    data=response.json()
    assert data['available'] is False
    assert data['account_id'] is None
    assert ('Selecione uma conta REAL' in data['reason'])
    read_balance.assert_not_awaited()
    read_profit.assert_not_awaited()


@pytest.mark.parametrize('path',('/api/real/balance','/api/real/results/today'))
def test_removed_real_account_does_not_fall_back_to_another(session_client,monkeypatch,path):
    client,factory=session_client
    set_selection(factory,'R999',mode='real')
    from app import real_readonly
    forbidden=AsyncMock(side_effect=AssertionError('wrong account opened'))
    monkeypatch.setattr(real_readonly,'fetch_real_balance',forbidden)
    monkeypatch.setattr(real_readonly,'fetch_real_daily_profit',forbidden)
    response=client.get(path)
    assert response.status_code==200
    data=response.json()
    assert data['available'] is False
    assert data['account_id'] is None
    assert 'não está disponível' in data['reason']
    forbidden.assert_not_awaited()


def test_readonly_balance_must_be_authenticated(session_client):
    client,_=session_client
    client.cookies.clear()
    assert client.get('/api/real/balance').status_code==401
    assert client.get('/api/real/results/today').status_code==401

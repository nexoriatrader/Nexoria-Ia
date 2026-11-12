from datetime import timedelta
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import main,db
from app.security import encrypt,hashsecret


def _client(tmp_path,monkeypatch):
    engine=create_engine(f'sqlite:///{tmp_path}/preview.sqlite')
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db,'SessionLocal',sessionmaker(bind=engine,expire_on_commit=False))
    with db.SessionLocal() as s:
        u=db.User(deriv_account_id='VDEMO-TEST',selected_mode='demo',stake='2.00')
        s.add(u);s.flush()
        s.add(db.Session(session_hash=hashsecret('preview_sid'),user_id=u.id,
                         token_encrypted=encrypt('mock-token'),
                         expires_at=db.utcnow()+timedelta(hours=1)))
        s.commit()
    client=TestClient(main.app)
    client.cookies.set('nexora_sid','preview_sid')
    return client


def test_preview_requires_auth_and_never_buys(monkeypatch):
    monkeypatch.setattr(main,'demo_buy',AsyncMock(side_effect=AssertionError('unexpected buy')))
    assert TestClient(main.app).get('/api/autopilot/demo-preview').status_code==401
    main.demo_buy.assert_not_awaited()


def test_insufficient_data_does_not_contact_deriv(tmp_path,monkeypatch):
    client=_client(tmp_path,monkeypatch)
    monkeypatch.setattr(main,'demo_proposal',AsyncMock(side_effect=AssertionError('unexpected proposal')))
    monkeypatch.setattr(main,'market_connected',True)
    monkeypatch.setattr(main,'recent_prices',[100.0]*10)
    main.runtimes.clear()
    result=client.get('/api/autopilot/demo-preview')
    assert result.status_code==200
    assert result.json()['proposal'] is None
    main.demo_proposal.assert_not_awaited()
    main.runtimes.clear()


def test_candidate_fetches_official_demo_proposal_without_buy(tmp_path,monkeypatch):
    client=_client(tmp_path,monkeypatch)
    monkeypatch.setattr(main,'market_connected',True)
    monkeypatch.setattr(main,'recent_prices',[100+i*.05 for i in range(100)])
    monkeypatch.setattr(main,'demo_buy',AsyncMock(side_effect=AssertionError('unexpected buy')))
    mock=AsyncMock(return_value={'proposal_id':'official-test-id','ask_price':'2','payout':'3.6',
                                 'currency':'USD','symbol':'1HZ100V','contract_type':'CALL',
                                 'duration_minutes':5,'demo_only':True,'bought':False})
    monkeypatch.setattr(main,'demo_proposal',mock)
    main.runtimes.clear()
    res=client.get('/api/autopilot/demo-preview')
    assert res.status_code==200,res.text
    body=res.json()
    assert body['state']=='DEMO_QUOTE_RESEARCH'
    assert body['proposal']['proposal_id']=='official-test-id'
    assert body['bought'] is False
    assert body['evaluation']['state']=='UNVALIDATED'
    assert mock.await_count==1
    main.demo_buy.assert_not_awaited()
    main.runtimes.clear()


def test_preview_rejects_disconnected_feed(tmp_path,monkeypatch):
    client=_client(tmp_path,monkeypatch)
    monkeypatch.setattr(main,'market_connected',False)
    main.runtimes.clear()
    assert client.get('/api/autopilot/demo-preview').status_code==409
    main.runtimes.clear()

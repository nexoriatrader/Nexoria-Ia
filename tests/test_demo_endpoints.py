import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main, db
from app.demo import APP_ID
from app.security import encrypt, hashsecret


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine=create_engine(f"sqlite:///{tmp_path}/db.sqlite")
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,"SessionLocal",factory)
    monkeypatch.setattr(main.db,"SessionLocal",factory)
    client=TestClient(main.app)
    yield client, factory
    engine.dispose()


def add_user(factory, *, account_id="V333", mode="demo"):
    sid=f"sid-{account_id}"
    with factory() as s:
        user=db.User(deriv_account_id=account_id,selected_mode=mode,stake="2.00",
                     stop_win="20.00",stop_loss="10.00")
        s.add(user)
        s.flush()
        s.add(db.Session(session_hash=hashsecret(sid),user_id=user.id,
                         token_encrypted=encrypt("test-token"),
                         expires_at=db.utcnow()+timedelta(hours=1)))
        s.commit()
    return sid


def test_buy_disabled_without_remote_order(api, monkeypatch):
    client,factory=api
    sid=add_user(factory)
    monkeypatch.setenv("NEXORA_DEMO_BUY_ENABLED","false")
    buy=AsyncMock(side_effect=AssertionError("buy unexpectedly called"))
    monkeypatch.setattr(main,"demo_buy",buy)
    client.cookies.set("nexora_sid",sid)

    resp=client.post("/api/demo/buy",json={"confirm":"DEMO_ONLY","request_key":"valid-key-01",
        "symbol":"1HZ100V","direction":"CALL","minutes":5})

    assert resp.status_code==503
    assert "desabilitada" in resp.json()["detail"]
    assert not buy.called
    with factory() as s:
        assert s.query(db.DemoOrder).count()==0


def test_confirmed_demo_buy_is_reserved_and_replay_does_not_buy_twice(api, monkeypatch):
    client,factory=api
    sid=add_user(factory)
    monkeypatch.setenv("NEXORA_DEMO_BUY_ENABLED","true")
    buy=AsyncMock(return_value={"contract_id":"123456","buy_price":"2.00"})
    monkeypatch.setattr(main,"demo_buy",buy)
    client.cookies.set("nexora_sid",sid)
    payload={"confirm":"DEMO_ONLY","request_key":"stable-key-01","symbol":"1HZ100V",
             "direction":"CALL","minutes":5}

    first=client.post("/api/demo/buy",json=payload)
    replay=client.post("/api/demo/buy",json=payload)

    assert first.status_code==200
    assert first.json()["state"]=="CONFIRMED"
    assert first.json()["demo_only"] is True
    assert replay.status_code==200
    assert replay.json()["replayed"] is True
    assert replay.json()["no_new_buy"] is True
    assert buy.call_count==1
    with factory() as s:
        order=s.query(db.DemoOrder).one()
        assert order.contract_id=="123456"
        assert order.state=="CONFIRMED"


def test_real_mode_cannot_buy_even_when_demo_flag_is_enabled(api, monkeypatch):
    client,factory=api
    sid=add_user(factory,account_id="R333",mode="real")
    monkeypatch.setenv("NEXORA_DEMO_BUY_ENABLED","true")
    buy=AsyncMock(side_effect=AssertionError("real mode reached broker buy"))
    monkeypatch.setattr(main,"demo_buy",buy)
    client.cookies.set("nexora_sid",sid)

    response=client.post("/api/demo/buy",json={"confirm":"DEMO_ONLY","request_key":"real-key-01",
        "symbol":"1HZ100V","direction":"CALL","minutes":5})

    assert response.status_code==403
    assert not buy.called
    with factory() as s:
        assert s.query(db.DemoOrder).count()==0


def test_unknown_buy_result_blocks_any_new_order(api, monkeypatch):
    client,factory=api
    sid=add_user(factory)
    monkeypatch.setenv("NEXORA_DEMO_BUY_ENABLED","true")
    buy=AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(main,"demo_buy",buy)
    client.cookies.set("nexora_sid",sid)

    first=client.post("/api/demo/buy",json={"confirm":"DEMO_ONLY","request_key":"unknown-key-01",
        "symbol":"1HZ100V","direction":"CALL","minutes":5})
    second=client.post("/api/demo/buy",json={"confirm":"DEMO_ONLY","request_key":"another-key-02",
        "symbol":"1HZ100V","direction":"PUT","minutes":5})

    assert first.status_code==502
    assert second.status_code==409
    assert buy.call_count==1
    with factory() as s:
        order=s.query(db.DemoOrder).one()
        assert order.state=="UNKNOWN"
        assert order.active_slot is not None


def test_demo_balance_requires_demo_session_and_is_read_only(api, monkeypatch):
    client,factory=api
    sid=add_user(factory)
    balance=AsyncMock(return_value={"balance":"1000.00","currency":"USD","demo_only":True})
    monkeypatch.setattr(main,"demo_account_balance",balance)
    client.cookies.set("nexora_sid",sid)

    response=client.get("/api/demo/balance")

    assert response.status_code==200
    assert response.json()["demo_only"] is True
    balance.assert_awaited_once_with("test-token","V333")


def test_auto_reconcile_persists_only_broker_settlement(api, monkeypatch):
    _,factory=api
    sid=add_user(factory)
    with factory() as s:
        user=s.query(db.User).one()
        order=db.DemoOrder(user_id=user.id,request_key="confirmed-key-01",active_slot=user.id,
            state="CONFIRMED",contract_id="24680",symbol="1HZ100V",direction="CALL",
            duration_minutes=5,stake="2.00",buy_price="2.00",
            created_at=db.utcnow()-timedelta(minutes=6))
        s.add(order)
        s.commit()
        order_id=order.id
    status=AsyncMock(return_value={"demo_only":True,"contract":{"contract_id":"24680",
        "is_sold":1,"profit":"1.25","status":"won"}})
    monkeypatch.setattr(main,"demo_contract_status",status)

    asyncio.run(main.reconcile_confirmed_orders_once())

    status.assert_awaited_once_with("test-token","V333","24680")
    with factory() as s:
        order=s.get(db.DemoOrder,order_id)
        assert order.state=="SETTLED"
        assert order.pnl=="1.25"
        assert order.active_slot is None


def test_history_and_demo_routes_require_session(api):
    client,_=api
    for path,method in [
        ("/api/demo/capabilities","get"),
        ("/api/demo/balance","get"),
        ("/api/demo/orders","get"),
        ("/api/demo/contract/234","get"),
        ("/api/demo/orders/1/reconcile","post"),
    ]:
        response=getattr(client,method)(path)
        assert response.status_code==401


def test_confirmation_persistence_failure_marks_order_unknown_and_does_not_retry(api, monkeypatch):
    client,factory=api
    sid=add_user(factory)
    monkeypatch.setenv("NEXORA_DEMO_BUY_ENABLED","true")
    buy=AsyncMock(return_value={"contract_id":"998877","buy_price":"2.00"})
    monkeypatch.setattr(main,"demo_buy",buy)
    monkeypatch.setattr(main,"confirmed",lambda *args,**kwargs: (_ for _ in ()).throw(RuntimeError("db write failed")))
    client.cookies.set("nexora_sid",sid)

    response=client.post("/api/demo/buy",json={"confirm":"DEMO_ONLY","request_key":"persist-key-01",
        "symbol":"1HZ100V","direction":"CALL","minutes":5})

    assert response.status_code==503
    assert buy.call_count==1
    with factory() as s:
        order=s.query(db.DemoOrder).one()
        assert order.state=="UNKNOWN"
        assert order.contract_id is None
        assert order.active_slot is not None


def test_unknown_without_contract_id_stays_blocked_from_automatic_reconcile(api, monkeypatch):
    client,factory=api
    sid=add_user(factory)
    with factory() as s:
        user=s.query(db.User).one()
        order=db.DemoOrder(user_id=user.id,request_key="unknown-key-02",active_slot=user.id,
            state="UNKNOWN",symbol="1HZ100V",direction="CALL",duration_minutes=5,stake="2.00")
        s.add(order)
        s.commit()
        order_id=order.id
    status=AsyncMock(side_effect=AssertionError("unknown order must not be guessed"))
    monkeypatch.setattr(main,"demo_contract_status",status)
    client.cookies.set("nexora_sid",sid)

    response=client.post(f"/api/demo/orders/{order_id}/reconcile")

    assert response.status_code==409
    assert "sem id confirmado" in response.json()["detail"].lower()
    assert not status.called
    asyncio.run(main.reconcile_confirmed_orders_once())
    assert not status.called


def test_recovery_route_requires_authenticated_session(api):
    client,_=api
    response=client.post('/api/orders/recover')
    assert response.status_code==401


def test_incomplete_deriv_snapshot_never_releases_unknown_reservation(api,monkeypatch):
    client,factory=api
    sid=add_user(factory)
    with factory() as s:
        user=s.query(db.User).one()
        order=db.DemoOrder(user_id=user.id,request_key='recovery-unknown-01',
            active_slot=user.id,state='UNKNOWN',symbol='1HZ100V',direction='CALL',
            duration_minutes=5,stake='2.00',created_at=db.utcnow()-timedelta(minutes=10))
        s.add(order);s.commit();order_id=order.id
    snapshot=AsyncMock(side_effect=main.RecoverySnapshotError('snapshot incompleto'))
    monkeypatch.setattr(main,'account_recovery_snapshot',snapshot)
    client.cookies.set('nexora_sid',sid)

    response=client.post('/api/orders/recover')

    assert response.status_code==502
    with factory() as s:
        order=s.get(db.DemoOrder,order_id)
        assert order.state=='UNKNOWN' and order.active_slot is not None
    snapshot.assert_awaited_once()


def test_recovery_marks_absent_order_cancelled_without_deleting_audit(api,monkeypatch):
    client,factory=api
    sid=add_user(factory)
    with factory() as s:
        user=s.query(db.User).one()
        order=db.DemoOrder(user_id=user.id,request_key='recovery-absent-01',
            active_slot=user.id,state='UNKNOWN',symbol='1HZ100V',direction='CALL',
            duration_minutes=5,stake='2.00',created_at=db.utcnow()-timedelta(minutes=10),
            updated_at=db.utcnow()-timedelta(minutes=10))
        s.add(order);s.commit();order_id=order.id
    snapshot=AsyncMock(return_value={'contracts':[],'transactions':[],'complete':True})
    monkeypatch.setattr(main,'account_recovery_snapshot',snapshot)
    client.cookies.set('nexora_sid',sid)

    response=client.post('/api/orders/recover')

    assert response.status_code==200,response.text
    assert response.json()['released']==1
    with factory() as s:
        order=s.get(db.DemoOrder,order_id)
        assert order is not None
        assert order.state=='CANCELLED' and order.active_slot is None
        assert order.broker_status=='confirmed_absent'


def test_recent_order_is_not_released_even_when_snapshot_is_empty(api,monkeypatch):
    client,factory=api
    sid=add_user(factory)
    with factory() as s:
        user=s.query(db.User).one()
        order=db.DemoOrder(user_id=user.id,request_key='recovery-recent-01',
            active_slot=user.id,state='UNKNOWN',symbol='1HZ100V',direction='CALL',
            duration_minutes=5,stake='2.00',created_at=db.utcnow()-timedelta(minutes=10),
            updated_at=db.utcnow()-timedelta(seconds=30))
        s.add(order);s.commit();order_id=order.id
    monkeypatch.setattr(main,'account_recovery_snapshot',AsyncMock(return_value={
        'contracts':[],'transactions':[],'complete':True,
    }))
    client.cookies.set('nexora_sid',sid)

    response=client.post('/api/orders/recover')

    assert response.status_code==200
    assert response.json()['released']==0
    assert response.json()['blocked']==1
    with factory() as s:
        order=s.get(db.DemoOrder,order_id)
        assert order.state=='UNKNOWN' and order.active_slot is not None


def test_unknown_transition_during_snapshot_cannot_use_old_reservation_age(api,monkeypatch):
    client,factory=api
    sid=add_user(factory)
    with factory() as s:
        user=s.query(db.User).one()
        order=db.DemoOrder(user_id=user.id,request_key='recovery-pending-01',
            active_slot=user.id,state='PENDING',symbol='1HZ100V',direction='CALL',
            duration_minutes=5,stake='2.00',created_at=db.utcnow()-timedelta(minutes=20),
            updated_at=db.utcnow()-timedelta(minutes=20))
        s.add(order);s.commit();order_id=order.id
    async def snapshot_after_buy_timeout(*args,**kwargs):
        with factory() as s:
            current=s.get(db.DemoOrder,order_id)
            current.state='UNKNOWN'
            current.updated_at=db.utcnow()
            s.commit()
        return {'contracts':[],'transactions':[],'complete':True}
    monkeypatch.setattr(main,'account_recovery_snapshot',snapshot_after_buy_timeout)
    client.cookies.set('nexora_sid',sid)

    response=client.post('/api/orders/recover')

    assert response.status_code==200
    assert response.json()['released']==0 and response.json()['blocked']==1
    with factory() as s:
        order=s.get(db.DemoOrder,order_id)
        assert order.state=='UNKNOWN' and order.active_slot is not None


def test_pending_buy_is_never_released_even_when_reservation_is_old(api,monkeypatch):
    client,factory=api
    sid=add_user(factory)
    with factory() as s:
        user=s.query(db.User).one()
        order=db.DemoOrder(user_id=user.id,request_key='recovery-pending-01',
            active_slot=user.id,state='PENDING',symbol='1HZ100V',direction='CALL',
            duration_minutes=5,stake='2.00',created_at=db.utcnow()-timedelta(minutes=20),
            updated_at=db.utcnow()-timedelta(minutes=20))
        s.add(order);s.commit();order_id=order.id
    monkeypatch.setattr(main,'account_recovery_snapshot',AsyncMock(return_value={
        'contracts':[],'transactions':[],'complete':True,
    }))
    client.cookies.set('nexora_sid',sid)

    response=client.post('/api/orders/recover')

    assert response.status_code==200
    assert response.json()['released']==0 and response.json()['blocked']==1
    with factory() as s:
        order=s.get(db.DemoOrder,order_id)
        assert order.state=='PENDING' and order.active_slot is not None


def test_real_recovery_requires_existing_profile_and_consent(api,monkeypatch):
    client,factory=api
    sid=add_user(factory,mode='real')
    with factory() as s:
        user=s.query(db.User).one()
        s.add(db.AutomationProfile(user_id=user.id,account_mode='real',
            real_account_id='CR123',market='derived',symbols_json='[]',strategy='trend',
            expiry_minutes=0,indicators_json='[]',direction='AUTO',min_payout='88',
            news_filter=False))
        s.commit()
    forbidden=AsyncMock(side_effect=AssertionError('real validation must not be skipped'))
    monkeypatch.setattr(main,'account_recovery_snapshot',forbidden)
    client.cookies.set('nexora_sid',sid)

    response=client.post('/api/orders/recover')

    assert response.status_code==403
    assert not forbidden.called


def test_recovery_links_unique_broker_contract_but_keeps_open_slot(api,monkeypatch):
    client,factory=api
    sid=add_user(factory)
    with factory() as s:
        user=s.query(db.User).one()
        order=db.DemoOrder(user_id=user.id,request_key='recovery-match-01',
            active_slot=user.id,state='UNKNOWN',symbol='1HZ100V',direction='CALL',
            duration_minutes=5,stake='2.00',created_at=db.utcnow()-timedelta(seconds=30))
        s.add(order);s.commit();order_id=order.id;created=order.created_at
    epoch=int(created.replace(tzinfo=timezone.utc).timestamp()) if created.tzinfo is None else int(created.timestamp())
    snapshot=AsyncMock(return_value={'contracts':[{
        'contract_id':24680,'underlying_symbol':'1HZ100V','contract_type':'CALL',
        'buy_price':'2.00','purchase_time':epoch,
    }],'transactions':[{
        'action_type':'buy','app_id':APP_ID,'amount':'-2.00',
        'shortcode':'CALL_1HZ100V','contract_id':'24680','transaction_time':epoch,
    }],'complete':True})
    status=AsyncMock(return_value={'account_mode':'demo','contract':{
        'contract_id':'24680','is_sold':0,'profit':'0',
    }})
    monkeypatch.setattr(main,'account_recovery_snapshot',snapshot)
    monkeypatch.setattr(main,'demo_contract_status',status)
    client.cookies.set('nexora_sid',sid)

    response=client.post('/api/orders/recover')

    assert response.status_code==200,response.text
    assert response.json()['linked']==1
    status.assert_awaited_once_with('test-token','V333','24680')
    with factory() as s:
        order=s.get(db.DemoOrder,order_id)
        assert order.state=='CONFIRMED' and order.contract_id=='24680'
        assert order.active_slot is not None


def test_demo_capabilities_never_enable_real_or_autopilot(api, monkeypatch):
    client,factory=api
    sid=add_user(factory)
    monkeypatch.setenv("NEXORA_DEMO_BUY_ENABLED","true")
    client.cookies.set("nexora_sid",sid)

    response=client.get("/api/demo/capabilities")

    assert response.status_code==200
    assert response.json()["manual_demo_purchase_enabled"] is True
    assert response.json()["real_orders_enabled"] is False
    assert response.json()["automatic_demo_orders_enabled"] is False


def test_health_reports_current_version_and_real_orders_disabled(api, monkeypatch):
    client,_=api
    monkeypatch.setenv("NEXORA_DEMO_BUY_ENABLED","false")

    response=client.get("/healthz")

    assert response.status_code==200
    assert response.json()["version"]==main.VERSION
    assert response.json()["execution"]=="DERIV_DEMO_ONLY"
    assert response.json()["real_orders_enabled"] is False

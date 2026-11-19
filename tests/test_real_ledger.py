"""REAL risk and durable reservation tests: all local, no broker purchases."""
from decimal import Decimal
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import db
from app.real_ledger import (
    reserve_real_order, confirm_real_order, mark_real_unknown,
    settle_real_order, RealOrderGuardError,
)

@pytest.fixture
def session():
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},
                         poolclass=StaticPool)
    db.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    with Session() as sess:
        sess.add(db.User(id=1,deriv_account_id="VRTC123",stake="1",
                         stop_loss="5",stop_win="0",selected_mode="demo"))
        sess.add(db.RealRobotConsent(user_id=1,account_id="CR123",
                   max_stake="2",stop_loss="5"))
        sess.add(db.AccountRiskSettings(user_id=1,account_mode="real",
                   account_id="CR123",stake="1",stop_win="0",stop_loss="5"))
        sess.commit()
        yield sess
    engine.dispose()

def place(sess,key="key-12345678",stake="1"):
    return reserve_real_order(sess,sess.get(db.User,1),account_id="CR123",
           request_key=key,symbol="R_100",direction="CALL",
           duration=5,stake=stake,currency="USD")

def test_real_order_reservation_is_durable_and_idempotent(session):
    order,created=place(session)
    assert created and order.state=="PENDING"
    same,created=place(session)
    assert not created and same.id==order.id
    with pytest.raises(RealOrderGuardError,match="contrato aberto"):
        place(session,"next-12345678")

def test_real_order_without_consent_is_blocked(session):
    consent=session.get(db.RealRobotConsent,1)
    consent.revoked_at=db.utcnow()
    session.commit()
    with pytest.raises(RealOrderGuardError,match="Autorização"):
        place(session)

def test_real_risk_enforces_stake_cap_and_stop(session):
    with pytest.raises(RealOrderGuardError,match="limite"):
        place(session,stake="4")
    consent=session.get(db.RealRobotConsent,1)
    settings=session.get(db.AccountRiskSettings,(1,'real','CR123'))
    consent.stop_loss="0"
    settings.stop_loss="0"
    session.commit()
    with pytest.raises(RealOrderGuardError,match="Stop Loss"):
        place(session)
    consent.stop_loss="0.50"
    settings.stop_loss="0.50"
    session.commit()
    with pytest.raises(RealOrderGuardError,match="Stop Loss"):
        place(session,stake="1")

def test_uncertain_order_never_releases_real_account_slot(session):
    order,_=place(session)
    mark_real_unknown(session,order)
    assert order.state=="UNKNOWN"
    assert order.active_slot=="CR123"
    with pytest.raises(RealOrderGuardError,match="contrato aberto"):
        place(session,"second-12345678")

def test_real_contract_settlement_releases_slot_only_with_broker_proof(session):
    order,_=place(session)
    confirm_real_order(session,order,{"account_mode":"real",
        "contract_id":"123456789","buy_price":"1","broker_purchase_time":1760000000})
    assert not settle_real_order(session,order,{"contract_id":"123456789",
        "is_sold":0,"profit":"1"})
    with pytest.raises(RealOrderGuardError,match="não corresponde"):
        settle_real_order(session,order,{"contract_id":"111","is_sold":1,"profit":"1"})
    assert order.active_slot=="CR123"
    assert settle_real_order(session,order,{"contract_id":"123456789",
        "is_sold":1,"profit":"1","status":"won"})
    assert order.active_slot is None and order.pnl=="1"
    second,created=place(session,"second-12345678")
    assert created and second.id!=order.id


def test_real_worker_reserves_and_reconciles_mock_contract(session,monkeypatch):
    """One simulated REAL execution: no external API calls or real money."""
    import asyncio
    import time
    import json
    from unittest.mock import AsyncMock
    from app import real_runtime
    from app.real_broker import RealPurchaseUncertain
    session.add(db.AutomationProfile(
        user_id=1,account_mode="real",real_account_id="CR123",
        market="derived",symbols_json=json.dumps(["R_100"]),strategy="trend",
        expiry_minutes=0,indicators_json="[]",direction="AUTO",
        min_payout="88",news_filter=False))
    session.commit()
    session_factory=sessionmaker(bind=session.bind,expire_on_commit=False)
    monkeypatch.setattr(db,"SessionLocal",session_factory)
    monkeypatch.setattr(real_runtime,"latest_token",lambda sess,uid:"fake-token")
    monkeypatch.setattr(real_runtime,"real_available",lambda:True)
    monkeypatch.setattr(real_runtime,"access_status",lambda sess,uid:{"allowed":True})
    bar=int(time.time())-60
    candidate={"symbol":"R_100","direction":"CALL","minutes":1,
               "expiry_options":[1,3,5,15],"last_bar_epoch":bar}
    monkeypatch.setattr(real_runtime,"confirmed_automation_signal",
                        AsyncMock(return_value=candidate))
    buy=AsyncMock(return_value={"account_mode":"real","contract_id":"123456789",
             "buy_price":"1","broker_purchase_time":1760000000})
    monkeypatch.setattr(real_runtime,"buy_real_contract",buy)
    monkeypatch.setattr(real_runtime,"real_contract_status",
                        AsyncMock(return_value={"account_mode":"real","contract":{
                          "contract_id":"123456789","is_sold":1,"profit":"0.88",
                          "status":"won"}}))
    real_runtime.running.add(1)
    async def check():
        await real_runtime._user_scan(1)
        assert session.query(db.RealOrder).count()==1, (real_runtime.reasons.get(1),list(real_runtime.running))
        order=session.query(db.RealOrder).one()
        assert order.state=="CONFIRMED"
        assert order.account_id=="CR123"
        assert buy.await_count==1
        await real_runtime.reconcile_real_once()
        session.expire_all()
        assert session.query(db.RealOrder).count()==1, (real_runtime.reasons.get(1),list(real_runtime.running))
        order=session.query(db.RealOrder).one()
        assert order.state=="SETTLED" and order.pnl=="0.88"
        assert order.active_slot is None
    try:
        asyncio.run(check())
    finally:
        real_runtime.running.clear()
        real_runtime.reasons.clear()
        real_runtime._user_locks.clear()
        real_runtime.last_scan.clear()
        session.close()


def test_uncertain_real_execution_never_sends_next_buy(session,monkeypatch):
    import asyncio
    import time
    import json
    from unittest.mock import AsyncMock
    from app import real_runtime
    from app.real_broker import RealPurchaseUncertain
    session.add(db.AutomationProfile(user_id=1,account_mode="real",
        real_account_id="CR123",market="derived",
        symbols_json=json.dumps(["R_100"]),strategy="trend",
        expiry_minutes=0,indicators_json="[]",direction="AUTO",
        min_payout="88",news_filter=False))
    session.commit()
    monkeypatch.setattr(db,"SessionLocal",
                        sessionmaker(bind=session.bind,expire_on_commit=False))
    monkeypatch.setattr(real_runtime,"latest_token",lambda sess,uid:"fake-token")
    monkeypatch.setattr(real_runtime,"real_available",lambda:True)
    monkeypatch.setattr(real_runtime,"access_status",lambda sess,uid:{"allowed":True})
    bar=int(time.time())-60
    monkeypatch.setattr(real_runtime,"confirmed_automation_signal",
                        AsyncMock(return_value={"symbol":"R_100","direction":"PUT",
                            "minutes":1,"expiry_options":[1],"last_bar_epoch":bar}))
    buy=AsyncMock(side_effect=RealPurchaseUncertain("unknown"))
    monkeypatch.setattr(real_runtime,"buy_real_contract",buy)
    real_runtime.running.add(1)
    async def check():
        await real_runtime._user_scan(1)
        session.expire_all()
        assert session.query(db.RealOrder).count()==1, (real_runtime.reasons.get(1),list(real_runtime.running))
        order=session.query(db.RealOrder).one()
        assert order.state=="UNKNOWN" and order.active_slot=="CR123"
        assert 1 not in real_runtime.running
        assert buy.await_count==1
        with pytest.raises(RealOrderGuardError):
            place(session,"another-12345678")
    try:
        asyncio.run(check())
    finally:
        real_runtime.running.clear()
        real_runtime.reasons.clear()
        real_runtime._user_locks.clear()
        real_runtime.last_scan.clear()
        session.close()


def test_real_reconcile_missing_official_purchase_time_still_checks_broker(session,monkeypatch):
    """Local reservation time triggers polling only; never fabricates P/L."""
    import asyncio
    from datetime import timedelta
    from unittest.mock import AsyncMock
    from app import real_runtime
    order,_=place(session)
    confirm_real_order(session,order,{"account_mode":"real",
        "contract_id":"555666777","buy_price":"1","broker_purchase_time":None})
    order.created_at=db.utcnow()-timedelta(minutes=20)
    session.commit()
    assert order.broker_purchase_at is None
    monkeypatch.setattr(db,"SessionLocal",
                        sessionmaker(bind=session.bind,expire_on_commit=False))
    monkeypatch.setattr(real_runtime,"latest_token",lambda sess,uid:"test-token")
    mocked=AsyncMock(return_value={"account_mode":"real","contract":{
        "contract_id":"555666777","is_sold":1,"profit":"-1","status":"lost"}})
    monkeypatch.setattr(real_runtime,"real_contract_status",mocked)
    async def check():
        await real_runtime.reconcile_real_once()
        assert mocked.await_count==1
    asyncio.run(check())
    session.expire_all()
    assert session.get(db.RealOrder,order.id).state=="SETTLED"
    assert session.get(db.RealOrder,order.id).pnl=="-1"


def test_real_entry_value_is_chosen_by_user_without_arbitrary_server_cap(session):
    consent=session.get(db.RealRobotConsent,1)
    consent.max_stake='50'
    consent.stop_loss='100'
    session.get(db.AccountRiskSettings,(1,'real','CR123')).stop_loss='100'
    session.commit()
    order,created=place(session,stake='25')
    assert created and order.stake=='25'
    assert order.state=='PENDING'


def test_user_daily_stop_applies_with_no_fixed_three_dollar_limit():
    from app.real_ledger import _risk_check
    from types import SimpleNamespace
    closed=[SimpleNamespace(state='SETTLED',pnl='-4',
              broker_purchase_at=db.utcnow(),created_at=db.utcnow())]
    # User selected 20 USD stop: a prior 4 USD loss does not block 2 USD.
    assert _risk_check(closed,'2','20','20')==Decimal('2')
    # User selected 5 USD stop: same exposure must be blocked.
    with pytest.raises(RealOrderGuardError,match='Stop Loss'):
        _risk_check(closed,'2','5','20')


def test_user_not_arbitrarily_limited_to_three_real_operations_per_day():
    from app.real_ledger import _risk_check
    from types import SimpleNamespace
    settled=[SimpleNamespace(state='SETTLED',pnl='0.20',
             broker_purchase_at=db.utcnow(),created_at=db.utcnow()) for _ in range(6)]
    assert _risk_check(settled,'1','20','1')==Decimal('1')


def test_user_stop_still_blocks_open_contract_even_without_global_caps():
    from app.real_ledger import _risk_check
    from types import SimpleNamespace
    open_order=SimpleNamespace(state='UNKNOWN',pnl=None,
         broker_purchase_at=None,created_at=db.utcnow())
    with pytest.raises(RealOrderGuardError,match='contrato aberto'):
        _risk_check([open_order],'1','20','1')


@pytest.mark.parametrize("saved,authorized", [
    ("3", "8"), ("8", "3"), ("0", "3"), ("5", "0"),
])
def test_real_stop_loss_mismatch_fails_closed_before_reservation(session,saved,authorized):
    """Mismatch NEVER silently chooses the larger/smaller stop."""
    account=session.get(db.AccountRiskSettings,(1,"real","CR123"))
    consent=session.get(db.RealRobotConsent,1)
    demo=session.get(db.User,1)
    demo.stop_loss="900.00"  # Must never govern REAL
    account.stop_loss=saved
    consent.stop_loss=authorized
    session.commit()
    with pytest.raises(RealOrderGuardError,match="divergente"):
        place(session,"mismatch-12345678")
    assert session.query(db.RealOrder).count()==0


def test_real_stop_loss_uses_selected_account_not_demo_user_values(session):
    """Saved REAL=7 + authorized=7, DEMO=100: stop 7 must block."""
    demo=session.get(db.User,1)
    demo.stop_loss="100"
    demo.stop_win="0.10"
    account=session.get(db.AccountRiskSettings,(1,"real","CR123"))
    account.stop_loss="7"
    account.stop_win="99"
    consent=session.get(db.RealRobotConsent,1)
    consent.stop_loss="7"
    session.add(db.RealOrder(
        user_id=1,account_id="CR123",request_key="settled-12345678",
        active_slot=None,state="SETTLED",contract_id="999111222",
        symbol="R_100",direction="CALL",duration_minutes=5,
        stake="6",currency="USD",pnl="-6.50",
        broker_purchase_at=db.utcnow()
    ))
    session.commit()
    with pytest.raises(RealOrderGuardError,match="Stop Loss"):
        place(session,"new-order-12345678",stake="1")
    assert session.query(db.RealOrder).count()==1


def test_real_stop_win_ignores_demo_stop_win_and_uses_account_value(session):
    demo=session.get(db.User,1)
    demo.stop_win="0"
    demo.stop_loss="1"
    account=session.get(db.AccountRiskSettings,(1,"real","CR123"))
    account.stop_win="2"
    account.stop_loss="8"
    consent=session.get(db.RealRobotConsent,1)
    consent.stop_loss="8"
    session.add(db.RealOrder(
        user_id=1,account_id="CR123",request_key="closed-12345678",
        active_slot=None,state="SETTLED",contract_id="999111223",
        symbol="R_100",direction="CALL",duration_minutes=5,
        stake="2",currency="USD",pnl="2.20",
        broker_purchase_at=db.utcnow()
    ))
    session.commit()
    with pytest.raises(RealOrderGuardError,match="Stop Win"):
        place(session,"next-win-12345678")
    assert session.query(db.RealOrder).count()==1


def test_demo_risk_checks_ignore_real_saved_stops_and_consent(session):
    from app.demo_ledger import order_guard,OrderGuardError
    demo=session.get(db.User,1)
    demo.stop_loss="1"
    demo.stop_win="0"
    account=session.get(db.AccountRiskSettings,(1,"real","CR123"))
    account.stop_loss="100"
    account.stop_win="100"
    consent=session.get(db.RealRobotConsent,1)
    consent.stop_loss="100"
    session.commit()
    # DEMO risk check uses ONLY user legacy DEMO stop_loss=1.
    with pytest.raises(OrderGuardError,match="Stop Loss"):
        order_guard(demo,[],"2")
    # REAL ledger ignores the intentionally divergent DEMO stop entirely.
    order,created=place(session,"isolated-12345678",stake="1")
    assert created and order.state=="PENDING"


def test_real_worker_disarms_stale_stop_snapshot_before_signal_or_broker_call(session,monkeypatch):
    """No broker/network calls when saved REAL and consent stop limits diverge."""
    import asyncio
    import json
    from unittest.mock import AsyncMock
    from app import real_runtime

    consent=session.get(db.RealRobotConsent,1)
    consent.stop_loss="9.00"
    configured=session.get(db.AccountRiskSettings,(1,"real","CR123"))
    configured.stop_loss="5.00"
    session.add(db.AutomationProfile(user_id=1,account_mode="real",
        real_account_id="CR123",market="derived",
        symbols_json=json.dumps(["R_100"]),strategy="trend",
        expiry_minutes=0,indicators_json="[]",direction="AUTO",
        min_payout="88",news_filter=False))
    session.commit()
    monkeypatch.setattr(db,"SessionLocal",sessionmaker(bind=session.bind,expire_on_commit=False))
    monkeypatch.setattr(real_runtime,"real_available",lambda:True)
    monkeypatch.setattr(real_runtime,"access_status",lambda sess,uid:{"allowed":True})
    monkeypatch.setattr(real_runtime,"latest_token",lambda sess,uid:"synthetic-token")
    signal=AsyncMock(side_effect=AssertionError("No scan with invalid stop authorization"))
    buy=AsyncMock(side_effect=AssertionError("No broker orders"))
    monkeypatch.setattr(real_runtime,"confirmed_automation_signal",signal)
    monkeypatch.setattr(real_runtime,"buy_real_contract",buy)
    real_runtime.last_scan.pop(1,None)
    real_runtime.running.add(1)
    try:
        asyncio.run(real_runtime._user_scan(1))
        assert 1 not in real_runtime.running
        assert "Stop Loss REAL" in real_runtime.reasons[1]
        assert session.query(db.RealOrder).count()==0
        signal.assert_not_awaited()
        buy.assert_not_awaited()
    finally:
        real_runtime.running.discard(1)
        real_runtime.last_scan.pop(1,None)
        real_runtime.reasons.pop(1,None)
        real_runtime._user_locks.clear()

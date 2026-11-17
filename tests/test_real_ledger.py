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
    consent.stop_loss="0"
    session.commit()
    with pytest.raises(RealOrderGuardError,match="obrigatório"):
        place(session)
    consent.stop_loss="0.50"
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

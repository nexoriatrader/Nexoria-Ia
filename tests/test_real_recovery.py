"""P0-02: synthetic-only, fail-closed recovery, idempotency and reentry guards.

All records are synthetic. Tests must not access Deriv or produce purchases.
"""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db, real_runtime
from app.real_ledger import RealOrderGuardError, public_real_order, reserve_real_order
from app.real_recovery import (
    DEFAULT_PENDING_GRACE_SECONDS,
    account_recovery_summary,
    classify_unresolved_real_order,
    promote_stale_pending_to_unknown,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/recovery.sqlite")
    db.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", Session)
    monkeypatch.setenv("NEXORA_REAL_AUTOPILOT_ENABLED", "false")
    monkeypatch.setenv("NEXORA_REAL_RELEASE_APPROVED", "false")
    with Session() as s:
        s.add(db.User(id=1,deriv_account_id="VREC01",selected_mode="demo",
                      stake="9",stop_win="999",stop_loss="888"))
        s.add(db.User(id=2,deriv_account_id="VREC02",selected_mode="demo"))
        s.add(db.RealRobotConsent(user_id=1,account_id="CR101",max_stake="2",
                                  stop_loss="10"))
        s.add(db.AccountRiskSettings(user_id=1,account_mode="real",
                 account_id="CR101",stake="1",stop_win="20",stop_loss="10"))
        s.commit()
    try:
        yield Session
    finally:
        engine.dispose()


def seed(session, *, uid=1, account="CR101", state="PENDING",
         active=None, contract=None, age_minutes=30, request="rkey-12345678"):
    age=db.utcnow()-timedelta(minutes=age_minutes)
    order=db.RealOrder(
        user_id=uid,account_id=account,request_key=request,
        active_slot=account if active is None else active,
        state=state,contract_id=contract,
        symbol="R_100",direction="CALL",duration_minutes=5,
        stake="1",currency="USD",created_at=age,updated_at=age)
    session.add(order)
    session.commit()
    return order.id


def place(session, request):
    return reserve_real_order(session, session.get(db.User,1),
         account_id="CR101",request_key=request,symbol="R_100",
         direction="CALL",duration=5,stake="1",currency="USD")


def test_expired_pending_becomes_unknown_but_never_releases_active_slot(repo):
    with repo() as s:
        oid=seed(s)
        before=s.get(db.RealOrder,oid)
        original=(before.account_id,before.contract_id,before.pnl,
                  before.active_slot,before.request_key)
        changed=promote_stale_pending_to_unknown(s)
        assert changed==[oid]
        s.expire_all()
        order=s.get(db.RealOrder,oid)
        assert order.state=="UNKNOWN"
        assert (order.account_id,order.contract_id,order.pnl,
                order.active_slot,order.request_key)==original
        assert order.error_summary is not None
        with pytest.raises(RealOrderGuardError,match="contrato aberto"):
            place(s,"fresh-12345678")
        same,created=place(s,"rkey-12345678")
        assert not created and same.id==oid


def test_repeat_recovery_does_not_rewrite_unknown_or_make_duplicate(repo):
    with repo() as s:
        oid=seed(s)
        assert promote_stale_pending_to_unknown(s)==[oid]
        order=s.get(db.RealOrder,oid)
        saved_updated=order.updated_at
        assert promote_stale_pending_to_unknown(s)==[]
        s.expire_all()
        assert s.get(db.RealOrder,oid).updated_at==saved_updated
        assert s.query(db.RealOrder).count()==1


def test_fresh_pending_keeps_original_state_and_slot(repo):
    with repo() as s:
        oid=seed(s,age_minutes=1)
        assert promote_stale_pending_to_unknown(s)==[]
        order=s.get(db.RealOrder,oid)
        assert order.state=="PENDING" and order.active_slot=="CR101"
        with pytest.raises(RealOrderGuardError,match="contrato aberto"):
            place(s,"another-12345678")


def test_stale_pending_with_known_contract_is_not_guessed_settled(repo):
    with repo() as s:
        oid=seed(s,contract="111222333")
        assert promote_stale_pending_to_unknown(s)==[oid]
        order=s.get(db.RealOrder,oid)
        assert order.state=="UNKNOWN"
        assert order.contract_id=="111222333"
        assert order.pnl is None and order.active_slot=="CR101"
        assert classify_unresolved_real_order(order)["status"]=="AWAITING_OFFICIAL_SETTLEMENT"


def test_final_states_unchanged_when_sweeping(repo):
    with repo() as s:
        settled=seed(s,state="SETTLED",request="done-12345678",active="",contract="321123321")
        cancelled=seed(s,state="CANCELLED",request="cancel-12345678",active="")
        assert promote_stale_pending_to_unknown(s)==[]
        assert s.get(db.RealOrder,settled).state=="SETTLED"
        assert s.get(db.RealOrder,cancelled).state=="CANCELLED"


def test_broken_slot_requires_integrity_review_not_automatic_release(repo):
    with repo() as s:
        oid=seed(s,active="WRONG_SLOT")
        assert promote_stale_pending_to_unknown(s)==[]
        order=s.get(db.RealOrder,oid)
        report=classify_unresolved_real_order(order)
        assert order.state=="PENDING"
        assert report["status"]=="INTEGRITY_REVIEW"
        assert report["needs_manual_review"]
        assert report["account_blocked"]


def test_unknown_without_contract_remains_manual_review_even_days_later(repo):
    with repo() as s:
        oid=seed(s,state="UNKNOWN",age_minutes=24*60*30)
        assert promote_stale_pending_to_unknown(s)==[]
        order=s.get(db.RealOrder,oid)
        report=classify_unresolved_real_order(order)
        assert report["status"]=="MANUAL_REVIEW_NO_CONTRACT"
        assert report["safe_to_retry"] is False
        assert order.active_slot=="CR101"
        assert public_real_order(order)["recovery"]["needs_manual_review"]


def test_summary_cannot_mix_accounts_or_users_or_demo(repo):
    with repo() as s:
        seed(s,account="CR101",uid=1)
        seed(s,account="CR202",uid=2,request="another-12345678")
        first=account_recovery_summary(s,1,"CR101")
        other=account_recovery_summary(s,2,"CR202")
        assert first["active_count"]==1 and first["blocked"]
        assert other["active_count"]==1 and other["blocked"]
        assert account_recovery_summary(s,1,"CR202")["active_count"]==0
        assert account_recovery_summary(s,2,"CR101")["active_count"]==0
        assert account_recovery_summary(s,1,None)["reason"]
        user=s.get(db.User,1)
        assert user.stop_loss=="888"


def test_no_auto_retry_or_broker_call_for_unknown_without_id(repo,monkeypatch):
    with repo() as s:
        oid=seed(s,state="UNKNOWN")
    buy=AsyncMock(side_effect=AssertionError("never submit broker orders"))
    official=AsyncMock(side_effect=AssertionError("no contract ID: no broker lookup"))
    monkeypatch.setattr(real_runtime,"buy_real_contract",buy)
    monkeypatch.setattr(real_runtime,"real_contract_status",official)
    monkeypatch.setattr(real_runtime,"latest_token",lambda session,uid:"synthetic")
    asyncio.run(real_runtime.reconcile_real_once())
    buy.assert_not_awaited()
    official.assert_not_awaited()
    with repo() as s:
        order=s.get(db.RealOrder,oid)
        assert order.state=="UNKNOWN" and order.active_slot=="CR101"


def test_real_reconcile_can_process_known_id_using_only_mocked_official_status(repo,monkeypatch):
    with repo() as s:
        oid=seed(s,contract="987654321")
    official=AsyncMock(return_value={"account_mode":"real","contract":{
        "contract_id":"987654321","is_sold":1,"profit":"0.55","status":"won"}})
    buy=AsyncMock(side_effect=AssertionError("never submit broker orders"))
    monkeypatch.setattr(real_runtime,"real_contract_status",official)
    monkeypatch.setattr(real_runtime,"buy_real_contract",buy)
    monkeypatch.setattr(real_runtime,"latest_token",lambda session,uid:"synthetic")
    asyncio.run(real_runtime.reconcile_real_once())
    with repo() as s:
        order=s.get(db.RealOrder,oid)
        assert order.state=="SETTLED" and order.active_slot is None
        assert order.pnl=="0.55"
        assert s.query(db.RealOrder).count()==1
    asyncio.run(real_runtime.reconcile_real_once())
    official.assert_awaited_once()
    buy.assert_not_awaited()


def test_unverified_official_status_never_releases_slot(repo,monkeypatch):
    with repo() as s:
        oid=seed(s,contract="987654321")
    official=AsyncMock(return_value={"account_mode":"real","contract":{
        "contract_id":"987654321","is_sold":0,"profit":"99","status":"open"}})
    monkeypatch.setattr(real_runtime,"real_contract_status",official)
    monkeypatch.setattr(real_runtime,"latest_token",lambda sess,uid:"synthetic")
    asyncio.run(real_runtime.reconcile_real_once())
    with repo() as s:
        order=s.get(db.RealOrder,oid)
        assert order.state=="UNKNOWN" and order.pnl is None
        assert order.active_slot=="CR101"


def test_mismatched_broker_contract_cannot_settle_or_release(repo,monkeypatch):
    with repo() as s:
        oid=seed(s,contract="987654321")
    official=AsyncMock(return_value={"account_mode":"real","contract":{
        "contract_id":"WRONG","is_sold":1,"profit":"1","status":"won"}})
    monkeypatch.setattr(real_runtime,"real_contract_status",official)
    monkeypatch.setattr(real_runtime,"latest_token",lambda sess,uid:"synthetic")
    asyncio.run(real_runtime.reconcile_real_once())
    with repo() as s:
        assert s.get(db.RealOrder,oid).state=="UNKNOWN"
        assert s.get(db.RealOrder,oid).active_slot=="CR101"
        assert s.get(db.RealOrder,oid).pnl is None


def test_restart_like_fresh_session_preserves_unresolved_hold(repo):
    with repo() as s:
        oid=seed(s)
        assert promote_stale_pending_to_unknown(s)==[oid]
    # An independent session simulates a fresh runtime after process restart.
    real_runtime.running.clear()
    with repo() as later:
        unresolved=later.get(db.RealOrder,oid)
        assert unresolved.state=="UNKNOWN" and unresolved.active_slot=="CR101"
        assert account_recovery_summary(later,1,"CR101")["blocked"]
        with pytest.raises(RealOrderGuardError,match="contrato aberto"):
            place(later,"restart-12345678")
    assert not real_runtime.real_available()


def test_invalid_recovery_parameters_are_rejected(repo):
    with repo() as s:
        with pytest.raises(ValueError):
            promote_stale_pending_to_unknown(s,grace_seconds=0)
        with pytest.raises(ValueError):
            promote_stale_pending_to_unknown(s,batch_size=101)
        assert s.query(db.RealOrder).count()==0

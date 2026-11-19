"""P0-03: safety-only state integrity and stale-session collision tests.

No broker adapter is imported or invoked in these tests; all rows are fictitious.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db
from app.demo_ledger import OrderGuardError, reserve as demo_reserve
from app.order_integrity import audit_real_account
from app.real_ledger import (
    RealOrderGuardError, reserve_real_order, confirm_real_order,
    cancel_unsubmitted_real_order, mark_real_unknown, settle_real_order,
)
from app.real_recovery import account_recovery_summary, promote_stale_pending_to_unknown


@pytest.fixture
def sessions(tmp_path):
    engine=create_engine(f"sqlite:///{tmp_path}/p003_integrity.sqlite")
    db.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    with Session() as s:
        s.add(db.User(id=1,deriv_account_id="V_SYNTH_003",selected_mode="demo",
                      stake="1",stop_win="0",stop_loss="10",martingale=False))
        s.add(db.RealRobotConsent(user_id=1,account_id="CR003",max_stake="2",
                                  stop_loss="10"))
        s.add(db.AccountRiskSettings(user_id=1,account_mode="real",
                                     account_id="CR003",stake="1",
                                     stop_win="20",stop_loss="10"))
        s.commit()
    try:
        yield Session
    finally:
        engine.dispose()


def _reserve(s,key="real-key-003",**kwargs):
    options=dict(account_id="CR003",request_key=key,symbol="R_100",
                 direction="CALL",duration=5,stake="1",currency="USD")
    options.update(kwargs)
    return reserve_real_order(s,s.get(db.User,1),**options)


def _receipt(contract_id="900000123"):
    return {"account_mode":"real","contract_id":contract_id,
            "buy_price":"1","broker_purchase_time":1760000000}


@pytest.mark.parametrize("override",[
    {"stake":"2"}, {"symbol":"R_75"}, {"direction":"PUT"},
    {"duration":3}, {"currency":"EUR"},
])
def test_real_reused_key_rejects_different_request_payload(sessions,override):
    with sessions() as s:
        first,created=_reserve(s)
        assert created
        with pytest.raises(RealOrderGuardError,match="idempotente"):
            _reserve(s,**override)
        same,replayed=_reserve(s)
        assert not replayed and same.id==first.id
        assert s.query(db.RealOrder).count()==1


@pytest.mark.parametrize("change",[
    {"symbol":"R_75"}, {"direction":"PUT"}, {"minutes":3},
    {"stake_override":"3"},
])
def test_demo_reused_key_rejects_conflicting_payload(sessions,change):
    with sessions() as s:
        original,_=demo_reserve(s,s.get(db.User,1),request_key="demo-key-003",
                                symbol="R_100",direction="CALL",minutes=5)
        opts=dict(request_key="demo-key-003",symbol="R_100",
                  direction="CALL",minutes=5)
        opts.update(change)
        with pytest.raises(OrderGuardError,match="reutilizada"):
            demo_reserve(s,s.get(db.User,1),**opts)
        assert s.query(db.DemoOrder).count()==1
        assert s.get(db.DemoOrder,original.id).active_slot==1


def test_demo_unchanged_request_keeps_existing_idempotency(sessions):
    with sessions() as s:
        user=s.get(db.User,1)
        original,created=demo_reserve(s,user,request_key="demo-legacy-123",
                symbol="R_100",direction="CALL",minutes=5)
        assert created
        same,replayed=demo_reserve(s,user,request_key="demo-legacy-123",
                symbol="R_100",direction="CALL",minutes=5)
        assert not replayed and same.id==original.id



def test_demo_default_stake_change_rejects_replayed_key_without_override(sessions):
    """Effective default stake changes 1 -> 2: replay must fail closed."""
    with sessions() as s:
        user=s.get(db.User,1)
        user.stake="1.00"
        s.commit()
        original,created=demo_reserve(
            s,user,request_key="demo-default-stake-003",
            symbol="R_100",direction="CALL",minutes=5
        )
        assert created and original.stake=="1.00"
        before=(original.id,original.request_key,original.state,
                original.active_slot,original.stake,original.contract_id,
                original.pnl,original.created_at)
        user.stake="2.00"
        s.commit()
        with pytest.raises(OrderGuardError,match="reutilizada com dados divergentes"):
            demo_reserve(
                s,user,request_key="demo-default-stake-003",
                symbol="R_100",direction="CALL",minutes=5
            )
        s.expire_all()
        persisted=s.get(db.DemoOrder,original.id)
        after=(persisted.id,persisted.request_key,persisted.state,
               persisted.active_slot,persisted.stake,persisted.contract_id,
               persisted.pnl,persisted.created_at)
        assert after==before
        assert s.query(db.DemoOrder).count()==1
        # An explicit override with the original amount remains a true replay.
        same,replayed=demo_reserve(
            s,user,request_key="demo-default-stake-003",
            symbol="R_100",direction="CALL",minutes=5,
            stake_override="1.0"
        )
        assert not replayed and same.id==original.id



def test_stale_unknown_writer_cannot_override_confirmed_record(sessions):
    with sessions() as creating:
        new,_=_reserve(creating)
        identifier=new.id
    with sessions() as current, sessions() as stale:
        fresh=current.get(db.RealOrder,identifier)
        old=stale.get(db.RealOrder,identifier)
        assert fresh.state==old.state=="PENDING"
        confirm_real_order(current,fresh,_receipt())
        mark_real_unknown(stale,old)
    with sessions() as verify:
        row=verify.get(db.RealOrder,identifier)
        assert row.state=="CONFIRMED"
        assert row.contract_id=="900000123"
        assert row.active_slot=="CR003"


def test_stale_cancellation_cannot_release_confirmed_slot(sessions):
    with sessions() as initial:
        row,_=_reserve(initial)
        identifier=row.id
    with sessions() as current, sessions() as stale:
        fresh=current.get(db.RealOrder,identifier)
        old=stale.get(db.RealOrder,identifier)
        confirm_real_order(current,fresh,_receipt())
        with pytest.raises(RealOrderGuardError,match="concorrente"):
            cancel_unsubmitted_real_order(stale,old,"stale cancellation")
    with sessions() as verify:
        row=verify.get(db.RealOrder,identifier)
        assert row.state=="CONFIRMED"
        assert row.contract_id=="900000123"
        assert row.active_slot=="CR003"


def test_stale_confirmation_cannot_undo_unknown_quarantine(sessions):
    from datetime import timedelta
    with sessions() as initial:
        row,_=_reserve(initial)
        identifier=row.id
        row.updated_at=db.utcnow()-timedelta(minutes=30)
        initial.commit()
    with sessions() as stale, sessions() as recovering:
        old=stale.get(db.RealOrder,identifier)
        assert promote_stale_pending_to_unknown(recovering)==[identifier]
        with pytest.raises(RealOrderGuardError,match="concorrência"):
            confirm_real_order(stale,old,_receipt())
    with sessions() as verify:
        row=verify.get(db.RealOrder,identifier)
        assert row.state=="UNKNOWN"
        assert row.contract_id is None and row.pnl is None
        assert row.active_slot=="CR003"


def test_only_first_settlement_can_persist_result(sessions):
    with sessions() as initial:
        order,_=_reserve(initial)
        confirm_real_order(initial,order,_receipt())
        identifier=order.id
    with sessions() as first, sessions() as delayed:
        one=first.get(db.RealOrder,identifier)
        two=delayed.get(db.RealOrder,identifier)
        assert settle_real_order(first,one,{
            "contract_id":"900000123","is_sold":1,"profit":"0.75","status":"won"})
        assert not settle_real_order(delayed,two,{
            "contract_id":"900000123","is_sold":1,"profit":"-999","status":"lost"})
    with sessions() as verify:
        row=verify.get(db.RealOrder,identifier)
        assert row.state=="SETTLED" and row.active_slot is None
        assert row.pnl=="0.75" and row.broker_status=="won"
        assert verify.query(db.RealOrder).count()==1


def test_corrupted_closed_record_blocks_new_reservation_fail_closed(sessions):
    with sessions() as s:
        s.add(db.RealOrder(user_id=1,account_id="CR003",
            request_key="bad-closed-123",active_slot=None,state="SETTLED",
            symbol="R_100",direction="CALL",duration_minutes=5,
            stake="1",currency="USD",contract_id="123654789",pnl=None))
        s.commit()
        scan=audit_real_account(s,1,"CR003")
        assert scan["requires_review"] and scan["issues"]["SETTLED_WITHOUT_EVIDENCE"]==1
        assert account_recovery_summary(s,1,"CR003")["blocked"] is True
        with pytest.raises(RealOrderGuardError,match="Integridade"):
            _reserve(s,"new-after-corrupted")


def test_confirmed_without_id_and_terminal_orphan_are_reported(sessions):
    with sessions() as s:
        s.add(db.RealOrder(user_id=1,account_id="CR003",
            request_key="missing-id-003",active_slot="CR003",state="CONFIRMED",
            symbol="R_100",direction="CALL",duration_minutes=5,
            stake="1",currency="USD"))
        s.add(db.RealOrder(user_id=1,account_id="CR003",
            request_key="orphan-slot-003",active_slot="CRORPHAN",state="CANCELLED",
            symbol="R_100",direction="PUT",duration_minutes=3,
            stake="1",currency="USD"))
        s.commit()
        scan=audit_real_account(s,1,"CR003")
        assert scan["issues"]["CONFIRMED_WITHOUT_CONTRACT_ID"]==1
        assert scan["issues"]["TERMINAL_SLOT_STILL_HELD"]==1
        assert scan["requires_review"]
        assert account_recovery_summary(s,1,"CR003")["blocked"] is True


def test_integrity_read_only_does_not_mutate_records(sessions):
    with sessions() as s:
        item,_=_reserve(s)
        created=item.created_at
        before=(item.state,item.active_slot,item.contract_id,item.pnl)
        for _ in range(3):
            status=audit_real_account(s,1,"CR003")
            assert status["records_checked"]==1
            assert status["issue_count"]==0
        s.expire_all()
        again=s.get(db.RealOrder,item.id)
        assert (again.state,again.active_slot,again.contract_id,again.pnl)==before
        assert again.created_at==created


def test_integrity_check_cannot_see_other_users_accounts(sessions):
    with sessions() as s:
        s.add(db.User(id=2,deriv_account_id="V_SYNTH_004",selected_mode="demo"))
        s.add(db.RealOrder(user_id=2,account_id="CROTHER",
            request_key="foreign-key-003",active_slot="CROTHER",state="UNKNOWN",
            symbol="R_100",direction="PUT",duration_minutes=3,
            stake="1",currency="USD"))
        s.commit()
        assert audit_real_account(s,1,"CR003")["records_checked"]==0
        assert audit_real_account(s,1,"CROTHER")["records_checked"]==0
        assert audit_real_account(s,2,"CROTHER")["active_count"]==1

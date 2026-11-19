"""P0-05 status contract regression tests — synthetic data, never broker I/O."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db, main, real_runtime
from app.demo_ledger import public_order
from app.real_ledger import public_real_order
from app.security import encrypt, hashsecret
from app.status_contract import (
    contract_stage, contract_summary, evaluate_user_status, running_evidence
)


class SyntheticLiveTask:
    def done(self):
        return False


class SyntheticFinishedTask:
    def done(self):
        return True


@pytest.fixture
def context(tmp_path, monkeypatch):
    engine=create_engine(f"sqlite:///{tmp_path}/status-p005.sqlite")
    db.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,"SessionLocal",Session)
    monkeypatch.setenv("NEXORA_REAL_AUTOPILOT_ENABLED","false")
    monkeypatch.setenv("NEXORA_REAL_RELEASE_APPROVED","false")
    monkeypatch.setenv("NEXORA_DEMO_BUY_ENABLED","false")
    monkeypatch.setenv("NEXORA_AUTOPILOT_DEMO_ENABLED","false")
    monkeypatch.setattr(main,"autopilot_task",None)
    monkeypatch.setattr(main,"real_task",None)
    never_buy=AsyncMock(side_effect=AssertionError("A status request must not call the broker"))
    monkeypatch.setattr(real_runtime,"buy_real_contract",never_buy)
    monkeypatch.setattr(main,"demo_buy",never_buy)
    with Session() as s:
        s.add(db.User(id=1,deriv_account_id="V_P005",selected_mode="demo",
                      stake="1.00",stop_win="10",stop_loss="5"))
        s.add(db.User(id=2,deriv_account_id="V_P005_OTHER",selected_mode="demo"))
        s.add(db.Session(user_id=1,session_hash=hashsecret("synthetic-p005-sid"),
                         token_encrypted=encrypt("synthetic-opaque-token"),
                         expires_at=db.utcnow()+timedelta(hours=1)))
        s.commit()
    try:
        client=TestClient(main.app)
        client.cookies.set("nexora_sid","synthetic-p005-sid")
        yield client,Session,never_buy,monkeypatch
    finally:
        main.autopilot_enabled.clear()
        real_runtime.reset_volatile_runtime_on_start()
        main.runtimes.clear()
        engine.dispose()


def seed_real(s, *, account="CRP005",state="CONFIRMED",key="real-p005-one",
              contract_id="509100123",pnl=None,user_id=1):
    o=db.RealOrder(user_id=user_id,account_id=account,request_key=key,
                   active_slot=account if state in ("PENDING","UNKNOWN","CONFIRMED") else None,
                   state=state,contract_id=contract_id,stake="1",currency="USD",
                   symbol="R_100",direction="CALL",duration_minutes=5,
                   pnl=pnl)
    s.add(o)
    s.flush()
    return o


def select_real(s,account="CRP005",*, with_consent=False):
    s.add(db.AutomationProfile(user_id=1,account_mode="real",
                               real_account_id=account))
    if with_consent:
        s.add(db.RealRobotConsent(user_id=1,account_id=account,
                                  max_stake="1",stop_loss="5"))
    s.commit()


@pytest.mark.parametrize("state,stage",[
    ("PENDING","RESERVED_UNCONFIRMED"),
    ("UNKNOWN","UNKNOWN_UNRESOLVED"),
    ("CONFIRMED","CONFIRMED_OPEN_UNSETTLED"),
    ("SETTLED","SETTLED"),
    ("CANCELLED","CANCELLED"),
    ("INVALID","UNKNOWN_UNVERIFIED"),
])
def test_contract_state_mapping_never_calls_confirmed_settled(state,stage):
    assert contract_stage(state)==stage
    assert stage!="SETTLED" if state!="SETTLED" else stage=="SETTLED"


def test_confirmed_real_contract_is_open_not_settled(context):
    client,Session,never_buy,_=context
    with Session() as s:
        select_real(s)
        row=seed_real(s)
        s.commit()
        public=public_real_order(row)
        assert public["state"]=="CONFIRMED"
        assert public["contract_stage"]=="CONFIRMED_OPEN_UNSETTLED"
        assert public["pnl"] is None
    response=client.get("/api/status")
    assert response.status_code==200,response.text
    data=response.json()
    assert data["contracts"]["confirmed_open_count"]==1
    assert data["contracts"]["settled_count"]==0
    assert data["contracts"]["open_unsettled"]==1
    assert data["contracts"]["overall_stage"]=="CONFIRMED_OPEN_UNSETTLED"
    assert data["reconciliation_pending"] is True
    assert data["real_capability"]["configured"] is True
    assert data["real_capability"]["available_for_execution"] is False
    never_buy.assert_not_awaited()


def test_demo_confirmed_and_settled_counted_separately(context):
    client,Session,_,_=context
    with Session() as s:
        for key,state,pnl,contract_id in [
            ("demo-confirmed-p005","CONFIRMED",None,"900500"),
            ("demo-settled-p005","SETTLED","0.65","900501")
        ]:
            s.add(db.DemoOrder(
                user_id=1,request_key=key,
                active_slot=1 if state=="CONFIRMED" else None,
                state=state,contract_id=contract_id,symbol="R_100",
                direction="CALL",duration_minutes=5,stake="1",pnl=pnl))
        s.commit()
        obj=s.query(db.DemoOrder).filter_by(state="CONFIRMED").one()
        assert public_order(obj)["contract_stage"]=="CONFIRMED_OPEN_UNSETTLED"
    data=client.get("/api/status").json()
    assert data["contracts"]["counts"]["CONFIRMED"]==1
    assert data["contracts"]["counts"]["SETTLED"]==1
    assert data["contracts"]["open_unsettled"]==1
    assert data["reconciliation_pending"] is True


def test_unknown_and_pending_never_look_liquidated(context):
    client,Session,_,_=context
    with Session() as s:
        select_real(s)
        seed_real(s,state="PENDING",key="p005-pending",
                  contract_id=None,account="CRP005")
        # Invalid duplicate live slot cannot coexist; inspect other accounts
        seed_real(s,state="UNKNOWN",key="p005-unknown",
                  contract_id=None,account="CRP005ALT")
        s.commit()
    status=client.get("/api/status").json()
    assert status["contracts"]["counts"]["PENDING"]==1
    assert status["contracts"]["counts"]["UNKNOWN"]==0
    assert status["contracts"]["settled_count"]==0
    assert status["contracts"]["open_unsettled"]==1
    with Session() as s:
        alt=contract_summary(s,1,account_mode="real",account_id="CRP005ALT")
        assert alt["counts"]["UNKNOWN"]==1
        assert alt["overall_stage"]=="UNKNOWN_UNRESOLVED"


def test_without_selection_real_contracts_are_unknown_not_zero(context):
    client,Session,_,_=context
    with Session() as s:
        select_real(s,account=None)
        seed_real(s,account="CRP005A",key="p005-a")
        seed_real(s,account="CRP005B",key="p005-b",contract_id="509100124")
        s.commit()
    status=client.get("/api/status").json()
    assert status["configured_mode"]=="real"
    assert status["contracts"]["known"] is False
    assert status["contracts"]["open_unsettled"] is None
    assert status["reconciliation_pending"] is None
    assert status["selected_account"] is None


def test_real_gates_consent_and_execution_independent(context):
    client,Session,_,_=context
    with Session() as s:
        select_real(s,with_consent=True)
    real_runtime.running.add(1)  # Simulates stale flag after a process restart.
    data=client.get("/api/status").json()
    assert data["running"] is True  # Legacy backward-compatibility.
    assert data["running_now"] is False  # Both production gates are OFF.
    assert data["real_capability"]["configured"] is True
    assert data["real_capability"]["technical_feature_enabled"] is False
    assert data["real_capability"]["administrative_release_approved"] is False
    assert data["real_capability"]["account_authorized"] is True
    assert data["real_capability"]["running_now"] is False
    assert data["running_evidence"]=="EXECUTION_GATE_DISABLED"


def test_revoked_real_consent_cannot_look_active_even_if_local_flag_set(context):
    client,Session,_,monkeypatch=context
    with Session() as s:
        select_real(s,with_consent=True)
        s.get(db.RealRobotConsent,1).revoked_at=db.utcnow()
        s.commit()
    real_runtime.running.add(1)
    monkeypatch.setattr(main,"real_task",SyntheticLiveTask())
    # Direct pure evaluator tests persistent consent independently of gates.
    with Session() as s:
        snapshot=evaluate_user_status(
            s,s.get(db.User,1),profile=s.get(db.AutomationProfile,1),
            local_demo=False,local_real=True,demo_scheduler=None,
            real_scheduler=SyntheticLiveTask(),
            demo_gate=False,real_gate=True,
            real_feature_gate=True,real_release_approved=True)
    assert snapshot["real_capability"]["account_authorized"] is False
    assert snapshot["running_now"] is False
    assert snapshot["running_evidence"]=="NO_PERSISTED_AUTHORIZATION"


def test_cross_process_worker_unknown_when_local_flag_absent(context):
    _,Session,_,_=context
    with Session() as s:
        select_real(s,with_consent=True)
        snapshot=evaluate_user_status(
            s,s.get(db.User,1),profile=s.get(db.AutomationProfile,1),
            local_demo=False,local_real=False,demo_scheduler=None,
            real_scheduler=None,demo_gate=False,real_gate=True,
            real_feature_gate=True,real_release_approved=True)
    assert snapshot["running_now"] is None
    assert snapshot["state_stale"] is True
    assert snapshot["running_evidence"]=="OTHER_PROCESS_OR_WORKER_UNVERIFIED"


def test_local_worker_only_confirmed_if_scheduler_and_authorization_exist(context):
    _,Session,_,_=context
    with Session() as s:
        select_real(s,with_consent=True)
        kwargs=dict(profile=s.get(db.AutomationProfile,1),
            local_demo=False,local_real=True,demo_scheduler=None,
            demo_gate=False,real_gate=True,real_feature_gate=True,
            real_release_approved=True)
        valid=evaluate_user_status(
            s,s.get(db.User,1),real_scheduler=SyntheticLiveTask(),**kwargs)
        finished=evaluate_user_status(
            s,s.get(db.User,1),real_scheduler=SyntheticFinishedTask(),**kwargs)
    assert valid["running_now"] is True
    assert finished["running_now"] is None


def test_demo_cross_worker_unknown_only_when_persisted_opt_in_exists(context):
    _,Session,_,_=context
    with Session() as s:
        u=s.get(db.User,1)
        s.add(db.RobotRun(user_id=1))
        s.add(db.AutoPilotResumeConsent(user_id=1))
        s.commit()
        vals=dict(profile=None,local_demo=False,local_real=False,
                  demo_scheduler=None,real_scheduler=None,
                  demo_gate=True,real_gate=False,
                  real_feature_gate=False,real_release_approved=False)
        unseen=evaluate_user_status(s,u,**vals)
    assert unseen["running_now"] is None
    assert unseen["running_evidence"]=="OTHER_PROCESS_OR_WORKER_UNVERIFIED"


def test_auth_public_health_does_not_expose_accounts_or_tokens(context):
    client,_,never_buy,_=context
    health=client.get("/healthz")
    assert health.status_code==200
    h=health.json()
    assert h["status_contract_version"]==2
    assert h["worker_status_scope"]=="PROCESS_LOCAL"
    assert h["real_orders_enabled"] is False
    assert "CRP005" not in health.text
    assert "synthetic-opaque-token" not in health.text
    unauth=TestClient(main.app)
    assert unauth.get("/api/session").json()["session_valid"] is False
    assert unauth.get("/api/status").status_code in (401,403)
    session=client.get("/api/session").json()
    assert session["authenticated"] is True and session["session_valid"] is True
    never_buy.assert_not_awaited()


def test_readiness_includes_conservative_fields_but_preserves_legacy(context):
    client,_,_,_=context
    resp=client.get("/api/autopilot/readiness")
    assert resp.status_code==200,resp.text
    data=resp.json()
    assert "ready" in data and "reason" in data and "access" in data
    assert data["execution"]=="DERIV_DEMO_ONLY"
    assert data["status_contract_version"]==2
    assert data["running_now"] is False


def test_status_contract_is_read_only_and_preserves_real_ledger(context):
    client,Session,never_buy,_=context
    with Session() as s:
        select_real(s,with_consent=True)
        o=seed_real(s)
        s.commit()
        original=(o.id,o.state,o.active_slot,o.contract_id,o.pnl,
                  o.updated_at.replace(tzinfo=None) if o.updated_at else None)
    for _ in range(3):
        data=client.get("/api/status").json()
        assert data["contracts"]["settled_count"]==0
        client.get("/api/autopilot/readiness")
    with Session() as s:
        o=s.query(db.RealOrder).one()
        assert (o.id,o.state,o.active_slot,o.contract_id,o.pnl,
                o.updated_at.replace(tzinfo=None) if o.updated_at else None)==original
        assert s.get(db.RealRobotConsent,1).revoked_at is None
    never_buy.assert_not_awaited()


def test_html_renders_unknown_and_explicit_confirmed_open_labels():
    source=(Path(__file__).resolve().parents[1]/"app"/"index.html").read_text()
    assert "id=\"dashOperationalDetails\"" in source
    assert "runningUnknown?'NÃO CONFIRMADO'" in source
    assert "CONFIRMED_OPEN_UNSETTLED:'Contrato confirmado · aberto · não liquidado'" in source
    assert "runningUnknown||(!robotRunning&&!available)" in source
    assert "'Não liquidado'" in source


@pytest.mark.parametrize("state,persisted,gate,selected,local,alive,expected",[
    ("no gate",True,False,True,True,True,False),
    ("no consent",False,True,True,True,True,False),
    ("no account",True,True,False,True,True,False),
    ("local active",True,True,True,True,True,True),
    ("local task missing",True,True,True,True,False,None),
    ("remote unknown",True,True,True,False,False,None),
])
def test_running_evidence_is_tri_state(state,persisted,gate,selected,local,alive,expected):
    r=running_evidence(local_running=local,worker_alive=alive,
        persisted_authorized=persisted,execution_gate=gate,
        account_selected=selected)
    assert r["running_now"] is expected, state
    assert r["state_stale"] is (expected is None)

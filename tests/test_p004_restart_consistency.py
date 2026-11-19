"""P0-04 restart safety: synthetic-only state, consent and status verification.

No Deriv tokens or broker calls. DEMO opt-in restoration is exercised with
stubbed guards; REAL never automatically resumes, even with stored consent.
"""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db, main, real_runtime
from app.security import encrypt, hashsecret
from app.real_recovery import account_recovery_summary


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/restart-p004.sqlite")
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setenv("NEXORA_REAL_AUTOPILOT_ENABLED", "false")
    monkeypatch.setenv("NEXORA_REAL_RELEASE_APPROVED", "false")
    monkeypatch.setenv("NEXORA_DEMO_BUY_ENABLED", "false")
    monkeypatch.setenv("NEXORA_AUTOPILOT_DEMO_ENABLED", "false")
    monkeypatch.setattr(main, "autopilot_allowed", lambda: False)
    real_buy = AsyncMock(side_effect=AssertionError("No REAL broker action in restart"))
    demo_buy = AsyncMock(side_effect=AssertionError("No DEMO broker action in restart"))
    monkeypatch.setattr(real_runtime, "buy_real_contract", real_buy)
    monkeypatch.setattr(main, "demo_buy", demo_buy)
    with sessions() as s:
        demo = db.User(id=1, deriv_account_id="V_RESTART_001",
                       selected_mode="demo", stake="1.00",
                       stop_win="5.00", stop_loss="3.00")
        s.add(demo)
        s.add(db.RealRobotConsent(user_id=1, account_id="CRP004",
                                   max_stake="1", stop_loss="3"))
        s.add(db.AccountRiskSettings(user_id=1, account_mode="real",
                     account_id="CRP004", stake="1", stop_win="7", stop_loss="3"))
        s.commit()
    try:
        yield sessions, engine, real_buy, demo_buy
    finally:
        main.autopilot_enabled.clear()
        main.autopilot_last_attempt.clear()
        main.v2_last_scan.clear()
        main.v2_gate_last_log.clear()
        main.automation_scan_progress.clear()
        main.autopilot_reasons.clear()
        main.runtimes.clear()
        real_runtime.reset_volatile_runtime_on_start()
        engine.dispose()


def make_order(s, account, key, state, contract_id=None):
    order = db.RealOrder(
        user_id=1, account_id=account, request_key=key,
        active_slot=account, state=state, contract_id=contract_id,
        symbol="R_100", direction="CALL", duration_minutes=5,
        stake="1", currency="USD"
    )
    s.add(order)
    s.commit()
    return order.id


def seed_demo_resume(s, *, profile_mode="demo", with_consent=True,
                     session_minutes=60):
    s.add(db.RobotRun(user_id=1, after_order_id=0))
    if with_consent:
        s.add(db.AutoPilotResumeConsent(user_id=1))
    s.add(db.AutomationProfile(user_id=1, account_mode=profile_mode))
    s.add(db.Session(
        session_hash=hashsecret("restart-p004-sid"), user_id=1,
        token_encrypted=encrypt("synthetic-session"),
        expires_at=db.utcnow()+timedelta(minutes=session_minutes)
    ))
    s.commit()


def allow_synthetic_demo_resume(monkeypatch):
    # Exercise existing DEMO resume logic without enabling any purchase gate.
    monkeypatch.setattr(main, "autopilot_allowed", lambda: True)
    monkeypatch.setattr(main, "access_status",
                        lambda sess, uid: {"allowed": True})
    monkeypatch.setattr(main, "autopilot_start_blocker",
                        lambda user, orders, **kwargs: None)
    monkeypatch.setattr(main, "autopilot_entry_plan",
                        lambda sess, user, orders: (None, None))


def test_restart_disarms_real_revokes_old_consent_and_preserves_orders(isolated):
    sessions, _, real_buy, demo_buy = isolated
    with sessions() as s:
        pending = make_order(s, "CRP004", "pending-00401", "PENDING")
        unknown = make_order(s, "CRP004B", "unknown-00401", "UNKNOWN")
        confirmed = make_order(s, "CRP004C", "known-00401", "CONFIRMED", "770044001")
        initial = {o.id: (o.state, o.active_slot, o.request_key,
                          o.contract_id, o.pnl)
                   for o in s.query(db.RealOrder).all()}
        assert len(initial)==3
    real_runtime.running.add(1)
    real_runtime.reasons[1]="previous-process-active"
    real_runtime.last_scan[1]=999
    real_runtime.user_lock(1)
    main.autopilot_last_attempt[1]=500
    main.v2_last_scan[1]=800
    main.automation_scan_progress[1]={"stage":"scanning"}
    main.autopilot_reasons[1]="cached"
    main.market_connected=True
    main.market_last_tick=999
    main.recent_prices.extend([123.0,124.0])
    report=main.prepare_service_restart()
    assert report=={"real_consents_revoked":1,"real_workers_running":False}
    assert not real_runtime.running
    assert not real_runtime.last_scan
    assert not real_runtime._user_locks
    assert not main.autopilot_enabled
    assert main.market_connected is False and main.market_last_tick==0
    assert not main.recent_prices and not main.automation_scan_progress
    with sessions() as fresh:
        consent=fresh.get(db.RealRobotConsent,1)
        assert consent.revoked_at is not None
        persisted={o.id:(o.state,o.active_slot,o.request_key,
                         o.contract_id,o.pnl)
                   for o in fresh.query(db.RealOrder).all()}
        assert persisted==initial
        for acc in ("CRP004","CRP004B","CRP004C"):
            assert account_recovery_summary(fresh,1,acc)["blocked"] is True
    assert main.prepare_service_restart()["real_consents_revoked"]==0
    real_buy.assert_not_awaited()
    demo_buy.assert_not_awaited()
    assert real_runtime.real_available() is False


def test_restart_without_opt_in_does_not_restore_demo_run(isolated, monkeypatch):
    sessions, _, _, _ = isolated
    with sessions() as s:
        seed_demo_resume(s, with_consent=False)
    allow_synthetic_demo_resume(monkeypatch)
    assert main.prepare_service_restart()["real_workers_running"] is False
    assert 1 not in main.autopilot_enabled
    with sessions() as s:
        assert s.get(db.RobotRun,1) is not None
        assert s.get(db.AutoPilotResumeConsent,1) is None
        assert s.get(db.RealRobotConsent,1).revoked_at is not None


def test_existing_explicit_demo_opt_in_restores_only_after_checks(isolated, monkeypatch):
    sessions, _, real_buy, demo_buy = isolated
    with sessions() as s:
        seed_demo_resume(s, with_consent=True)
    allow_synthetic_demo_resume(monkeypatch)
    result=main.prepare_service_restart()
    assert result["real_consents_revoked"]==1
    assert main.autopilot_enabled=={1}
    assert 1 not in real_runtime.running
    assert main.prepare_service_restart()["real_consents_revoked"]==0
    assert main.autopilot_enabled=={1}
    with sessions() as s:
        assert s.get(db.RobotRun,1) is not None
        assert s.get(db.AutoPilotResumeConsent,1) is not None
        assert s.get(db.RealRobotConsent,1).revoked_at is not None
    real_buy.assert_not_awaited()
    demo_buy.assert_not_awaited()


def test_real_selected_profile_cannot_restore_old_demo_consent(isolated, monkeypatch):
    sessions, _, _, _ = isolated
    with sessions() as s:
        seed_demo_resume(s, profile_mode="real", with_consent=True)
    allow_synthetic_demo_resume(monkeypatch)
    main.prepare_service_restart()
    assert not main.autopilot_enabled and not real_runtime.running
    with sessions() as s:
        assert s.get(db.AutoPilotResumeConsent,1) is None
        assert s.get(db.RobotRun,1) is not None
        assert s.get(db.RealRobotConsent,1).revoked_at is not None
    assert "configuração" in main.autopilot_reasons[1]


def test_expired_session_blocks_explicit_demo_resume(isolated, monkeypatch):
    sessions, _, _, _ = isolated
    with sessions() as s:
        seed_demo_resume(s, profile_mode="demo",
                         with_consent=True, session_minutes=-5)
    allow_synthetic_demo_resume(monkeypatch)
    main.prepare_service_restart()
    assert not main.autopilot_enabled
    with sessions() as s:
        assert s.get(db.AutoPilotResumeConsent,1) is None


def test_unresolved_demo_order_blocks_opt_in_restore(isolated, monkeypatch):
    sessions, _, _, _ = isolated
    with sessions() as s:
        seed_demo_resume(s, with_consent=True)
        s.add(db.DemoOrder(user_id=1,request_key="pending-demo-004",
                           active_slot=1,state="UNKNOWN",
                           symbol="R_100",direction="CALL",
                           duration_minutes=5,stake="1"))
        s.commit()
    allow_synthetic_demo_resume(monkeypatch)
    main.prepare_service_restart()
    assert not main.autopilot_enabled
    with sessions() as s:
        row=s.query(db.DemoOrder).one()
        assert row.state=="UNKNOWN" and row.active_slot==1
        assert s.get(db.AutoPilotResumeConsent,1) is None


def test_restart_db_error_fails_closed_before_demo_restore(isolated, monkeypatch):
    sessions, _, _, _ = isolated
    allow_synthetic_demo_resume(monkeypatch)
    restored=[]
    monkeypatch.setattr(main, "restore_demo_autopilot_consent",
                        lambda: restored.append(True))
    def fail_db(session, **kwargs):
        raise RuntimeError("synthetic PostgreSQL outage")
    monkeypatch.setattr(main, "revoke_stale_real_consent_after_restart", fail_db)
    main.autopilot_enabled.add(1)
    real_runtime.running.add(1)
    with pytest.raises(RuntimeError, match="synthetic PostgreSQL outage"):
        main.prepare_service_restart()
    assert not main.autopilot_enabled and not real_runtime.running
    assert restored==[]


def test_status_after_restart_does_not_claim_running_from_old_run(isolated):
    sessions, _, _, _ = isolated
    with sessions() as s:
        seed_demo_resume(s, with_consent=False)
    main.prepare_service_restart()
    client=TestClient(main.app)
    client.cookies.set("nexora_sid","restart-p004-sid")
    response=client.get("/api/status")
    assert response.status_code==200,response.text
    status=response.json()
    assert status["running"] is False
    assert status["interrupted_after_restart"] is True
    assert status["configured_account_mode"]=="demo"
    assert status["execution"]=="DERIV_DEMO_ONLY"


def test_lifespan_cancels_all_worker_tasks_including_public_selfcheck(isolated,monkeypatch):
    sessions, engine, real_buy, demo_buy = isolated
    async def idle():
        await asyncio.Event().wait()
    # Prevent all scheduler/network activity in a full ASGI lifespan test.
    for name in ("feed","reconcile_confirmed_loop","autopilot_loop",
                 "public_market_selfcheck_once","research_loop","m1_collect_loop"):
        monkeypatch.setattr(main,name,idle)
    monkeypatch.setattr(real_runtime,"real_loop",idle)
    monkeypatch.setattr(real_runtime,"reconcile_real_loop",idle)
    with TestClient(main.app):
        assert main.market_selfcheck_task is not None
        assert not main.market_selfcheck_task.done()
        assert not real_runtime.running
    assert main.market_selfcheck_task.cancelled()
    assert not main.market_task or main.market_task.cancelled()
    assert not main.autopilot_task or main.autopilot_task.cancelled()
    assert not main.real_task or main.real_task.cancelled()
    real_buy.assert_not_awaited()
    demo_buy.assert_not_awaited()


def test_poll_discards_stale_real_running_flag_after_external_revocation(isolated):
    sessions,_,real_buy,demo_buy=isolated
    real_runtime.running.add(1)
    with sessions() as external_process:
        row=external_process.get(db.RealRobotConsent,1)
        row.revoked_at=db.utcnow()
        external_process.commit()
    changed=real_runtime.disarm_revoked_real_workers()
    assert changed==[1]
    assert 1 not in real_runtime.running
    assert "revogada" in real_runtime.reasons[1]
    assert real_runtime.disarm_revoked_real_workers()==[]
    real_buy.assert_not_awaited()
    demo_buy.assert_not_awaited()


def test_poll_leaves_other_users_active_and_does_not_rewrite_consent(isolated):
    sessions,_,real_buy,_=isolated
    with sessions() as s:
        s.add(db.User(id=2,deriv_account_id="V_P004_SECOND",selected_mode="demo"))
        s.add(db.RealRobotConsent(user_id=2,account_id="CRP004SECOND",
                                  max_stake="1",stop_loss="3"))
        one=s.get(db.RealRobotConsent,1)
        one.revoked_at=db.utcnow()
        s.commit()
    real_runtime.running.update((1,2))
    assert real_runtime.disarm_revoked_real_workers()==[1]
    assert real_runtime.running=={2}
    with sessions() as fresh:
        assert fresh.get(db.RealRobotConsent,1).revoked_at is not None
        assert fresh.get(db.RealRobotConsent,2).revoked_at is None
    real_buy.assert_not_awaited()


def test_final_send_guard_fails_closed_on_sqlite_without_touching_orders(isolated):
    from app.restart_consistency import locked_real_consent_for_send
    sessions,_,real_buy,_=isolated
    with locked_real_consent_for_send(1,"CRP004") as authorized:
        assert authorized is False
    with sessions() as s:
        assert s.get(db.RealRobotConsent,1).revoked_at is None
        assert s.query(db.RealOrder).count()==0
    real_buy.assert_not_awaited()


def test_missing_real_consent_also_disarms_stale_process_flag(isolated):
    sessions,_,real_buy,_=isolated
    with sessions() as s:
        s.delete(s.get(db.RealRobotConsent,1))
        s.commit()
    real_runtime.running.add(1)
    assert real_runtime.disarm_revoked_real_workers()==[1]
    assert 1 not in real_runtime.running
    real_buy.assert_not_awaited()

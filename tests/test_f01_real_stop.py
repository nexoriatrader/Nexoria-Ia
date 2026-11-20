"""F01 REAL stop: authenticated, account-scoped and fail-closed regressions.

Synthetic SQLite fixtures for the HTTP/API contract only. Cross-process
serialization is separately exercised against disposable PostgreSQL.
REAL feature gates are never enabled; the broker is never called.
"""
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db, main, real_runtime
from app.security import encrypt, hashsecret


@pytest.fixture
def stop_env(tmp_path, monkeypatch):
    engine=create_engine(f"sqlite:///{tmp_path}/f01-stop.sqlite")
    db.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,"SessionLocal",Session)
    monkeypatch.setenv("NEXORA_REAL_AUTOPILOT_ENABLED","false")
    monkeypatch.setenv("NEXORA_REAL_RELEASE_APPROVED","false")
    monkeypatch.setenv("NEXORA_DEMO_BUY_ENABLED","false")
    monkeypatch.setenv("NEXORA_AUTOPILOT_DEMO_ENABLED","false")
    impossible=AsyncMock(side_effect=AssertionError("REAL broker call prohibited"))
    monkeypatch.setattr(real_runtime,"buy_real_contract",impossible)
    main.autopilot_enabled.clear()
    main.automation_scan_progress.clear()
    real_runtime.reset_volatile_runtime_on_start()
    with Session() as s:
        for uid,account in [(1,"CRF01OWN"),(2,"CRF01OTHER")]:
            s.add(db.User(id=uid,deriv_account_id=f"VF01{uid}",
                          selected_mode="demo"))
            s.add(db.RealRobotConsent(user_id=uid,account_id=account,
                                      max_stake="1",stop_loss="5"))
        s.add(db.Session(user_id=1,session_hash=hashsecret("f01-auth-1"),
                         token_encrypted=encrypt("fake-test-only-token"),
                         expires_at=db.utcnow()+timedelta(hours=1)))
        s.add(db.RobotRun(user_id=1))
        s.add(db.AutoPilotResumeConsent(user_id=1))
        s.add(db.RobotRun(user_id=2))
        s.add(db.AutoPilotResumeConsent(user_id=2))
        for uid,account,key,state,contract in [
            (1,"CRF01OWN","f01-pending","PENDING",None),
            (1,"CRF01OLDUNKNOWN","f01-unknown","UNKNOWN",None),
            (1,"CRF01OLDCONF","f01-confirmed","CONFIRMED","F010001"),
            (2,"CRF01OTHER","f01-foreign","PENDING",None),
        ]:
            s.add(db.RealOrder(
                user_id=uid,account_id=account,request_key=key,
                active_slot=account,state=state,contract_id=contract,
                symbol="R_100",direction="CALL",duration_minutes=5,
                stake="1",currency="USD"))
        s.commit()
    client=TestClient(main.app)
    client.cookies.set("nexora_sid","f01-auth-1")
    try:
        yield client,Session,impossible,monkeypatch
    finally:
        main.autopilot_enabled.clear()
        main.automation_scan_progress.clear()
        main.autopilot_reasons.clear()
        real_runtime.reset_volatile_runtime_on_start()
        engine.dispose()


def _snapshot(s):
    return sorted((
        x.id,x.user_id,x.account_id,x.request_key,x.state,
        x.active_slot,x.contract_id,x.pnl
    ) for x in s.query(db.RealOrder).all())


def test_real_stop_revokes_persistently_without_local_running_and_preserves_every_contract(stop_env):
    client,Session,broker,_=stop_env
    assert not real_runtime.running  # Like a different Render process.
    with Session() as s:
        original=_snapshot(s)
        foreign_before=s.get(db.RealRobotConsent,2).revoked_at
    response=client.post("/api/autopilot/stop")
    assert response.status_code==200,response.text
    data=response.json()
    assert data["running"] is False
    assert data["running_scope"]=="LOCAL_PROCESS_ONLY"
    assert data["running_now"] is None
    assert data["worker_shutdown_confirmed"] is None
    assert data["worker_shutdown_state"]=="UNKNOWN_REMOTE_WORKERS"
    # The DEMO-only profile has no active REAL worker; consent is revoked
    # defensively but must not relabel this DEMO shutdown as REAL.
    assert data["execution"]=="DERIV_DEMO_ONLY"
    assert data["real_revocation_status"]=="REVOKED_NOW"
    assert data["real_revocation_persisted"] is True
    assert data["real_authorization_absent"] is True
    with Session() as s:
        assert s.get(db.RealRobotConsent,1).revoked_at is not None
        assert s.get(db.RealRobotConsent,2).revoked_at==foreign_before
        assert _snapshot(s)==original
        assert s.get(db.RobotRun,1) is None
        assert s.get(db.AutoPilotResumeConsent,1) is None
        assert s.get(db.RobotRun,2) is not None
        assert s.get(db.AutoPilotResumeConsent,2) is not None
    broker.assert_not_awaited()


def test_stop_is_idempotent_when_consent_already_revoked(stop_env):
    client,Session,broker,_=stop_env
    first=client.post("/api/autopilot/stop")
    assert first.status_code==200
    with Session() as s:
        revoked_at=s.get(db.RealRobotConsent,1).revoked_at
        snapshot=_snapshot(s)
    second=client.post("/api/autopilot/stop")
    assert second.status_code==200
    result=second.json()
    assert result["real_revocation_status"]=="ALREADY_REVOKED"
    assert result["real_revocation_persisted"] is True
    assert result["execution"]=="DERIV_DEMO_ONLY"
    assert result["worker_shutdown_confirmed"] is None
    with Session() as s:
        assert s.get(db.RealRobotConsent,1).revoked_at==revoked_at
        assert _snapshot(s)==snapshot
    broker.assert_not_awaited()


def test_no_real_consent_keeps_existing_demo_stop_behavior(stop_env):
    client,Session,broker,_=stop_env
    with Session() as s:
        s.delete(s.get(db.RealRobotConsent,1))
        s.commit()
    main.autopilot_enabled.add(1)
    response=client.post("/api/autopilot/stop")
    assert response.status_code==200,response.text
    result=response.json()
    assert result["execution"]=="DERIV_DEMO_ONLY"
    assert result["real_revocation_status"]=="NO_CONSENT"
    assert result["real_revocation_persisted"] is None
    assert 1 not in main.autopilot_enabled
    with Session() as s:
        assert s.get(db.RealRobotConsent,1) is None
        assert s.get(db.RealRobotConsent,2).revoked_at is None
        assert s.get(db.AutoPilotResumeConsent,1) is None
    broker.assert_not_awaited()


def test_stop_real_and_demo_are_both_disarmed_without_touching_others(stop_env):
    client,Session,broker,_=stop_env
    main.autopilot_enabled.update({1,2})
    real_runtime.running.update({1,2})
    response=client.post("/api/autopilot/stop")
    assert response.status_code==200
    assert response.json()["real_revocation_status"]=="REVOKED_NOW"
    assert 1 not in main.autopilot_enabled
    assert 1 not in real_runtime.running
    assert 2 in main.autopilot_enabled
    assert 2 in real_runtime.running
    with Session() as s:
        assert s.get(db.RealRobotConsent,1).revoked_at is not None
        assert s.get(db.RealRobotConsent,2).revoked_at is None
    broker.assert_not_awaited()


def test_stop_requires_authentication_and_never_revokes_foreign_accounts(stop_env):
    client,Session,broker,_=stop_env
    with Session() as s:
        before=[(c.user_id,c.account_id,c.revoked_at)
                for c in s.query(db.RealRobotConsent).order_by(db.RealRobotConsent.user_id)]
    unauth=TestClient(main.app)
    response=unauth.post("/api/autopilot/stop")
    assert response.status_code in (401,403),response.text
    with Session() as s:
        after=[(c.user_id,c.account_id,c.revoked_at)
               for c in s.query(db.RealRobotConsent).order_by(db.RealRobotConsent.user_id)]
    assert before==after
    broker.assert_not_awaited()


def test_database_failure_never_returns_success_or_reports_workers_stopped(stop_env):
    client,Session,broker,monkeypatch=stop_env
    real_runtime.running.add(1)
    def failure(uid):
        raise RuntimeError("synthetic rollback")
    monkeypatch.setattr(real_runtime,"revoke_real_consent_for_stop",failure)
    response=client.post("/api/autopilot/stop")
    assert response.status_code==503,response.text
    assert 1 in real_runtime.running
    assert "workers" in response.json()["detail"]
    with Session() as s:
        assert s.get(db.RealRobotConsent,1).revoked_at is None
        assert s.get(db.AutoPilotResumeConsent,1) is not None
    broker.assert_not_awaited()


def test_historical_revoked_real_consent_cannot_relabel_demo_stop(stop_env):
    client,Session,broker,_=stop_env
    with Session() as s:
        row=s.get(db.RealRobotConsent,1)
        row.revoked_at=db.utcnow()
        s.add(db.AutomationProfile(user_id=1,account_mode='demo'))
        s.commit()
    response=client.post("/api/autopilot/stop")
    assert response.status_code==200,response.text
    result=response.json()
    assert result["execution"]=="DERIV_DEMO_ONLY"
    assert result["real_revocation_status"]=="ALREADY_REVOKED"
    assert result["demo_cleanup_status"]=="COMPLETED"
    assert result["partial_failure"] is False
    with Session() as s:
        assert s.get(db.RealRobotConsent,1).revoked_at is not None
        assert s.get(db.AutoPilotResumeConsent,1) is None
    broker.assert_not_awaited()


def test_explicit_real_profile_is_classified_as_real(stop_env):
    client,Session,broker,_=stop_env
    with Session() as s:
        s.add(db.AutomationProfile(
            user_id=1,account_mode='real',real_account_id='CRF01OWN'))
        s.commit()
    result=client.post("/api/autopilot/stop")
    assert result.status_code==200,result.text
    assert result.json()["execution"]=="DERIV_REAL"
    broker.assert_not_awaited()


def test_real_revocation_commit_survives_demo_cleanup_failure(stop_env):
    client,Session,broker,monkeypatch=stop_env
    def fail_cleanup(uid):
        raise RuntimeError("synthetic DEMO database failure")
    monkeypatch.setattr(main,"_f01_clear_demo_stop_records",fail_cleanup)
    response=client.post("/api/autopilot/stop")
    assert response.status_code==207,response.text
    data=response.json()
    assert data["real_revocation_status"]=="REVOKED_NOW"
    assert data["real_revocation_persisted"] is True
    assert data["real_authorization_absent"] is True
    assert data["demo_cleanup_status"]=="FAILED"
    assert data["partial_failure"] is True
    assert data["running"] is None
    assert data["worker_shutdown_confirmed"] is None
    with Session() as s:
        assert s.get(db.RealRobotConsent,1).revoked_at is not None
        assert s.get(db.AutoPilotResumeConsent,1) is not None
    broker.assert_not_awaited()


def test_missing_real_consent_stop_invalidates_old_start_generation(stop_env):
    client,Session,broker,_=stop_env
    with Session() as s:
        s.delete(s.get(db.RealRobotConsent,1))
        s.commit()
    epoch=real_runtime.capture_real_start_generation(1)
    assert epoch==(0,0)
    result=client.post("/api/autopilot/stop")
    assert result.status_code==200,result.text
    assert result.json()["real_revocation_status"]=="NO_CONSENT"
    with Session() as s:
        assert s.get(db.RealAuthorizationFence,1).generation==epoch[1]+1
        assert s.get(db.RealRobotConsent,1) is None
    broker.assert_not_awaited()


def test_repeated_stop_advances_fence_without_rewriting_revocation(stop_env):
    client,Session,_,_=stop_env
    assert client.post("/api/autopilot/stop").status_code==200
    with Session() as s:
        initial=s.get(db.RealAuthorizationFence,1).generation
        revoked=s.get(db.RealRobotConsent,1).revoked_at
    assert client.post("/api/autopilot/stop").status_code==200
    with Session() as s:
        assert s.get(db.RealAuthorizationFence,1).generation==initial+1
        assert s.get(db.RealRobotConsent,1).revoked_at==revoked


def test_slow_revocation_does_not_block_async_health_endpoint(stop_env):
    """STOP's synchronous PostgreSQL-style wait must run outside ASGI loop."""
    import asyncio
    import threading
    import time
    import httpx

    _,Session,broker,monkeypatch=stop_env
    started=threading.Event()
    original=real_runtime.revoke_real_consent_for_stop

    def simulate_slow_db(uid):
        started.set()
        time.sleep(0.7)
        return original(uid)

    monkeypatch.setattr(real_runtime,"revoke_real_consent_for_stop",
                        simulate_slow_db)

    async def scenario():
        transport=httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport,
                                    base_url="http://f01.test") as client:
            client.cookies.set("nexora_sid","f01-auth-1")
            task=asyncio.create_task(client.post("/api/autopilot/stop"))
            assert await asyncio.to_thread(started.wait,2.0)
            t0=time.monotonic()
            health=await asyncio.wait_for(client.get("/healthz"),timeout=0.5)
            elapsed=time.monotonic()-t0
            assert health.status_code==200
            assert elapsed<0.5
            response=await asyncio.wait_for(task,timeout=3.5)
            assert response.status_code==200,response.text
            assert response.json()["real_revocation_status"]=="REVOKED_NOW"

    asyncio.run(scenario())
    broker.assert_not_awaited()

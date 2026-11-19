"""F01 multi-process REAL STOP verification on disposable PostgreSQL only.

This test does NOT enable REAL feature environment gates, obtain credentials,
contact Deriv or create financial operations. A mocked worker prepares only
synthetic database reservations and any invocation of the broker stub fails.
"""
import asyncio
import multiprocessing
import os
import time
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app import db
from app.security import encrypt, hashsecret


def _dsn():
    if os.getenv("NEXORA_P0_EPHEMERAL_PG") != "yes":
        pytest.skip("Disposable CI PostgreSQL only")
    raw=os.environ["NEXORA_P0_TEST_DATABASE_URL"]
    url=make_url(raw)
    if not (url.drivername=="postgresql+psycopg"
            and url.host in ("127.0.0.1","localhost")
            and url.port==5432 and url.database=="nexora_p0_ci"
            and url.username=="p0_ci"):
        pytest.fail("Refusing non-disposable PostgreSQL connection")
    assert os.getenv("NEXORA_REAL_AUTOPILOT_ENABLED","false")=="false"
    assert os.getenv("NEXORA_REAL_RELEASE_APPROVED","false")=="false"
    return raw


def _make_user_and_consent(s, name, account, *, cookie=None, with_profile=True):
    user=db.User(deriv_account_id=name,selected_mode="demo",
                 stake="1",stop_win="0",stop_loss="0")
    s.add(user)
    s.flush()
    uid=user.id
    s.add(db.RealRobotConsent(
        user_id=uid,account_id=account,max_stake="2",stop_loss="6"))
    if with_profile:
        s.add(db.AccountRiskSettings(
            user_id=uid,account_mode="real",account_id=account,
            stake="1",stop_win="0",stop_loss="6"))
        s.add(db.AutomationProfile(
            user_id=uid,account_mode="real",real_account_id=account,
            market="derived",strategy="trend",symbols_json='["R_100"]',
            news_filter=False,expiry_minutes=0,min_payout="88"))
    if cookie:
        s.add(db.Session(
            session_hash=hashsecret(cookie),user_id=uid,
            token_encrypted=encrypt("f01-synthetic-never-used"),
            expires_at=db.utcnow()+timedelta(hours=2)))
    s.commit()
    return uid


def _f01_worker_pausing_after_reservation(dsn,uid,account,reserved,release,results):
    from app import real_runtime
    engine=create_engine(dsn,pool_pre_ping=True)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    old=db.SessionLocal
    db.SessionLocal=Session
    original_reserve=real_runtime.reserve_real_order
    calls=[]

    async def fake_signal(profile):
        return {"symbol":"R_100","direction":"CALL",
                "last_bar_epoch":time.time()-60,
                "expiry_options":[5]}

    async def forbidden_broker(*args,**kwargs):
        calls.append("UNEXPECTED_BROKER")
        raise AssertionError("F01 prohibits all broker calls")

    def reserve_then_pause(*args,**kwargs):
        order,created=original_reserve(*args,**kwargs)
        assert created, "synthetic reservation not created"
        reserved.set()
        if not release.wait(timeout=35):
            raise RuntimeError("F01 test timeout waiting for stop")
        return order,created

    try:
        # Local function stubs are not financial authorizations: the actual
        # environment remains OFF and the broker is an always-failing fake.
        real_runtime.real_available=lambda:True
        real_runtime.access_status=lambda session,user_id:{"allowed":True}
        real_runtime.latest_token=lambda session,user_id:"f01-fake-token"
        real_runtime.confirmed_automation_signal=fake_signal
        real_runtime._candidate_window=lambda candidate:time.time()+40
        real_runtime.buy_real_contract=forbidden_broker
        real_runtime.reserve_real_order=reserve_then_pause
        real_runtime.running.add(uid)
        asyncio.run(real_runtime._user_scan(uid))
        with Session() as sess:
            rows=sess.scalars(select(db.RealOrder).where(
                db.RealOrder.user_id==uid,
                db.RealOrder.account_id==account)).all()
            results.put(("ok",len(calls),uid in real_runtime.running,
                         [(r.state,r.active_slot,r.contract_id,r.pnl) for r in rows]))
    except Exception as exc:
        results.put(("error",type(exc).__name__+":"+str(exc)[:250]))
    finally:
        db.SessionLocal=old
        real_runtime.running.discard(uid)
        engine.dispose()


def _f01_http_stop_process(dsn,cookie,ready,reply):
    from fastapi.testclient import TestClient
    from app import main, real_runtime
    engine=create_engine(dsn,pool_pre_ping=True)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    old=db.SessionLocal
    db.SessionLocal=Session
    try:
        assert not real_runtime.running, "Stop must run without local REAL flag"
        client=TestClient(main.app)
        client.cookies.set("nexora_sid",cookie)
        ready.set()
        response=client.post("/api/autopilot/stop")
        reply.put(("ok",response.status_code,response.json()))
    except Exception as exc:
        reply.put(("error",type(exc).__name__+":"+str(exc)[:240]))
    finally:
        db.SessionLocal=old
        engine.dispose()


def _real_rows(s,uid):
    return sorted((r.id,r.user_id,r.account_id,r.state,
                   r.active_slot,r.contract_id,r.pnl)
                  for r in s.scalars(select(db.RealOrder).where(
                    db.RealOrder.user_id==uid)).all())


def test_stop_on_different_process_revokes_before_scanner_can_send():
    dsn=_dsn()
    engine=create_engine(dsn,pool_pre_ping=True)
    db.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    ctx=multiprocessing.get_context("spawn")
    reserved,release,route_ready=ctx.Event(),ctx.Event(),ctx.Event()
    output,stop_reply=ctx.Queue(),ctx.Queue()
    worker=stopper=None
    cookie="f01-multiprocess-session"
    account="CRF01PGSCAN"
    try:
        with Session() as s:
            uid=_make_user_and_consent(s,"VF01PGSCAN",account,cookie=cookie)
            other=_make_user_and_consent(s,"VF01PGFOREIGN","CRF01PGFOREIGN")
            for acc,key,state,cid in [
                ("CRF01PGUNKNOWN","f01-pg-unknown","UNKNOWN",None),
                ("CRF01PGCONFIRMED","f01-pg-confirmed","CONFIRMED","F01PG001"),
            ]:
                s.add(db.RealOrder(
                    user_id=uid,account_id=acc,request_key=key,
                    active_slot=acc,state=state,contract_id=cid,
                    symbol="R_100",direction="PUT",duration_minutes=5,
                    stake="1",currency="USD"))
            s.commit()
            existing=_real_rows(s,uid)
        worker=ctx.Process(target=_f01_worker_pausing_after_reservation,
                           args=(dsn,uid,account,reserved,release,output))
        worker.start()
        assert reserved.wait(timeout=40), "synthetic worker never reserved"
        with Session() as s:
            before=_real_rows(s,uid)
            assert len(before)==len(existing)+1
            own=s.scalar(select(db.RealOrder).where(
                db.RealOrder.user_id==uid,
                db.RealOrder.account_id==account))
            assert own.state=="PENDING" and own.active_slot==account
        stopper=ctx.Process(target=_f01_http_stop_process,
                            args=(dsn,cookie,route_ready,stop_reply))
        stopper.start()
        assert route_ready.wait(timeout=30)
        stopped=stop_reply.get(timeout=40)
        assert stopped[0]=="ok",stopped
        assert stopped[1]==200,stopped
        body=stopped[2]
        assert body["real_revocation_status"]=="REVOKED_NOW"
        assert body["real_revocation_persisted"] is True
        assert body["worker_shutdown_confirmed"] is None
        assert body["running_now"] is None
        assert body["running_scope"]=="LOCAL_PROCESS_ONLY"
        with Session() as s:
            assert s.get(db.RealRobotConsent,uid).revoked_at is not None
            assert s.get(db.RealRobotConsent,other).revoked_at is None
            assert _real_rows(s,uid)==before
        # Revocation has COMMITTED; now let worker reach the final send guard.
        release.set()
        observed=output.get(timeout=40)
        worker.join(timeout=30)
        stopper.join(timeout=30)
        assert worker.exitcode==0 and stopper.exitcode==0
        assert observed==("ok",0,False,[("PENDING",account,None,None)]),observed
        with Session() as s:
            assert _real_rows(s,uid)==before
            assert s.get(db.RealRobotConsent,other).revoked_at is None
    finally:
        release.set()
        for proc in (worker,stopper):
            if proc:
                proc.join(timeout=3)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=5)
        output.close()
        stop_reply.close()
        engine.dispose()


def _f01_hold_send_lock(dsn,uid,account,entered,release,events):
    from app import restart_consistency
    engine=create_engine(dsn,pool_pre_ping=True)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    old=db.SessionLocal
    db.SessionLocal=Session
    try:
        with restart_consistency.locked_real_consent_for_send(uid,account) as valid:
            assert valid
            entered.set()
            assert release.wait(timeout=35)
            events.put(("synthetic_before_stop",time.monotonic_ns()))
        events.put(("lock_released",time.monotonic_ns()))
    except Exception as exc:
        events.put(("error",type(exc).__name__+":"+str(exc)[:200]))
    finally:
        db.SessionLocal=old
        engine.dispose()


def test_stop_serializes_with_existing_postgres_send_guard():
    """Committed stop cannot interleave between guard and fake send marker."""
    import queue
    dsn=_dsn()
    engine=create_engine(dsn,pool_pre_ping=True)
    db.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    ctx=multiprocessing.get_context("spawn")
    entered,release,route_ready=ctx.Event(),ctx.Event(),ctx.Event()
    events,stop_reply=ctx.Queue(),ctx.Queue()
    holder=stopper=None
    try:
        cookie="f01-lock-synthetic-session"
        account="CRF01LOCK"
        with Session() as s:
            uid=_make_user_and_consent(s,"VF01LOCK",account,cookie=cookie)
        holder=ctx.Process(target=_f01_hold_send_lock,
                           args=(dsn,uid,account,entered,release,events))
        holder.start()
        assert entered.wait(timeout=35)
        stopper=ctx.Process(target=_f01_http_stop_process,
                            args=(dsn,cookie,route_ready,stop_reply))
        stopper.start()
        assert route_ready.wait(timeout=30)
        # A committed revocation must not be observable until the prior
        # guarded window exits. No broker call is made by the holder.
        with pytest.raises(queue.Empty):
            stop_reply.get(timeout=0.45)
        with Session() as s:
            assert s.get(db.RealRobotConsent,uid).revoked_at is None
        release.set()
        start_marker=events.get(timeout=40)
        lock_release=events.get(timeout=40)
        response=stop_reply.get(timeout=40)
        holder.join(timeout=30)
        stopper.join(timeout=30)
        assert holder.exitcode==0 and stopper.exitcode==0
        assert start_marker[0]=="synthetic_before_stop",start_marker
        assert lock_release[0]=="lock_released",lock_release
        assert response[0]=="ok" and response[1]==200,response
        assert response[2]["real_revocation_status"]=="REVOKED_NOW"
        assert response[2]["worker_shutdown_confirmed"] is None
        with Session() as s:
            assert s.get(db.RealRobotConsent,uid).revoked_at is not None
            assert not s.scalars(select(db.RealOrder).where(
                db.RealOrder.user_id==uid)).all()
    finally:
        release.set()
        for proc in (holder,stopper):
            if proc:
                proc.join(timeout=3)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=5)
        events.close()
        stop_reply.close()
        engine.dispose()

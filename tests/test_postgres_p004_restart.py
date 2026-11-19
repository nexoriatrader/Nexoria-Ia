"""P0-04: restart consistency verified only against ephemeral localhost Postgres.

No broker network, authorization activation, Supabase or production database.
Independent spawned processes simulate repeated server cold starts.
"""
import multiprocessing
import os

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app import db
from app.restart_consistency import revoke_stale_real_consent_after_restart


def _safe_dsn():
    if os.getenv("NEXORA_P0_EPHEMERAL_PG") != "yes":
        pytest.skip("Only an isolated disposable PostgreSQL database is allowed")
    dsn=os.environ["NEXORA_P0_TEST_DATABASE_URL"]
    url=make_url(dsn)
    if not (url.drivername=="postgresql+psycopg"
            and url.host in ("127.0.0.1","localhost")
            and url.port==5432 and url.database=="nexora_p0_ci"
            and url.username=="p0_ci"):
        pytest.fail("Refusing any non-disposable PostgreSQL connection")
    return dsn


def _restart_worker(dsn, ready, signal, replies):
    """Simulates independent process start using only SQLAlchemy and consent."""
    engine=create_engine(dsn,pool_pre_ping=True)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    try:
        ready.put(True)
        if not signal.wait(timeout=30):
            replies.put(("error","start timeout"))
            return
        with Session() as session:
            count=revoke_stale_real_consent_after_restart(session)
            replies.put(("ok",count))
    except Exception as exc:
        replies.put(("error",type(exc).__name__))
    finally:
        engine.dispose()


def _run_two_restarts(dsn):
    ctx=multiprocessing.get_context("spawn")
    ready, replies=ctx.Queue(),ctx.Queue()
    signal=ctx.Event()
    processes=[ctx.Process(target=_restart_worker,
                           args=(dsn,ready,signal,replies)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=40)
        signal.set()
        result=[replies.get(timeout=40) for _ in processes]
        for process in processes:
            process.join(timeout=40)
            assert process.exitcode==0
        return result
    finally:
        signal.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        ready.close()
        replies.close()


def test_postgresql_restart_preserves_unresolved_contracts_and_old_progression():
    dsn=_safe_dsn()
    engine=create_engine(dsn,pool_pre_ping=True)
    db.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    try:
        with Session() as s:
            user=db.User(deriv_account_id="V_P004_PG_PERSIST",selected_mode="demo",
                         stake="2.50",stop_win="5",stop_loss="3")
            s.add(user)
            s.flush()
            uid=user.id
            s.add(db.RealRobotConsent(user_id=uid,account_id="CRP004PG",
                       max_stake="1.25",stop_loss="6"))
            s.add(db.RobotRun(user_id=uid,after_order_id=17))
            s.add(db.AutoPilotResumeConsent(user_id=uid))
            s.add(db.DemoOrder(user_id=uid,request_key="pg-demo-004",
                state="SETTLED",active_slot=None,contract_id="D990004",
                symbol="R_100",direction="CALL",duration_minutes=5,
                stake="2.50",pnl="-2.50"))
            s.add(db.RealOrder(user_id=uid,account_id="CRP004PG",
                request_key="pg-real-004",state="UNKNOWN",
                active_slot="CRP004PG",contract_id=None,
                symbol="R_100",direction="PUT",duration_minutes=5,
                stake="1.25",currency="USD"))
            s.commit()
        # The ephemeral CI service also contains consent fixtures from
        # earlier tests. Startup revokes *all* stale REAL consents, not just
        # this scenario's user, so compare with the actual pre-start count.
        with Session() as before_start:
            expected=sum(1 for consent in before_start.scalars(
                select(db.RealRobotConsent)).all() if consent.revoked_at is None)
            assert expected>=1
        with Session() as first:
            assert revoke_stale_real_consent_after_restart(first)==expected
        with Session() as fresh:
            consent=fresh.get(db.RealRobotConsent,uid)
            assert consent.revoked_at is not None
            order=fresh.scalar(select(db.RealOrder).where(
                db.RealOrder.user_id==uid))
            assert order.state=="UNKNOWN" and order.active_slot=="CRP004PG"
            assert order.contract_id is None and order.pnl is None
            demo=fresh.scalar(select(db.DemoOrder).where(
                db.DemoOrder.user_id==uid))
            assert demo.state=="SETTLED" and demo.pnl=="-2.50"
            assert fresh.get(db.RobotRun,uid).after_order_id==17
            assert fresh.get(db.AutoPilotResumeConsent,uid) is not None
            assert fresh.get(db.User,uid).stake=="2.50"
        with Session() as repeat:
            assert revoke_stale_real_consent_after_restart(repeat)==0
    finally:
        engine.dispose()


def test_two_independent_postgres_restart_processes_revoke_once():
    dsn=_safe_dsn()
    engine=create_engine(dsn,pool_pre_ping=True)
    db.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    try:
        with Session() as s:
            user=db.User(deriv_account_id="V_P004_PG_WORKERS",selected_mode="demo")
            s.add(user)
            s.flush()
            uid=user.id
            s.add(db.RealRobotConsent(user_id=uid,account_id="CRP004MULTI",
                                      max_stake="1",stop_loss="3"))
            s.add(db.RealOrder(user_id=uid,account_id="CRP004MULTI",
                               request_key="pg-multi-004",state="PENDING",
                               active_slot="CRP004MULTI",symbol="R_100",
                               direction="CALL",duration_minutes=5,
                               stake="1",currency="USD"))
            s.commit()
        outcomes=_run_two_restarts(dsn)
        assert sorted(outcomes)==[("ok",0),("ok",1)],outcomes
        with Session() as verify:
            assert verify.get(db.RealRobotConsent,uid).revoked_at is not None
            order=verify.scalar(select(db.RealOrder).where(db.RealOrder.user_id==uid))
            assert order.state=="PENDING"
            assert order.active_slot=="CRP004MULTI"
            assert order.contract_id is None
            assert order.pnl is None
            assert len(verify.scalars(select(db.RealOrder).where(
                db.RealOrder.user_id==uid)).all())==1
    finally:
        engine.dispose()


def _pending_scan_process(dsn, uid, account_id, reserved, continue_scan, result):
    """REAL scanner with every external dependency replaced by an in-memory fake.

    The sole purpose is to prove a committed revocation stops dispatch.
    It may create ONE synthetic PostgreSQL PENDING reservation, never a trade.
    """
    import asyncio
    import time
    from app import real_runtime
    from app.real_ledger import reserve_real_order

    engine=create_engine(dsn,pool_pre_ping=True)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    original_local=db.SessionLocal
    db.SessionLocal=Session
    sent_calls=[]
    original_reserve=real_runtime.reserve_real_order

    async def fake_signal(profile):
        return {"symbol":"R_100","direction":"CALL",
                "last_bar_epoch":time.time()-60,
                "expiry_options":[5]}

    async def impossible_buy(*args,**kwargs):
        sent_calls.append("CALLED")
        raise AssertionError("No broker submission permitted in this test")

    def pause_after_reserve(*args,**kwargs):
        order,created=original_reserve(*args,**kwargs)
        assert created
        reserved.set()
        if not continue_scan.wait(timeout=25):
            raise RuntimeError("Synthetic revocation test timed out")
        return order,created

    try:
        real_runtime.real_available=lambda: True  # isolated child-only mock, no env gates
        real_runtime.access_status=lambda session,user_id: {"allowed":True}
        real_runtime.latest_token=lambda session,user_id:"synthetic-not-a-token"
        real_runtime.confirmed_automation_signal=fake_signal
        real_runtime.buy_real_contract=impossible_buy
        real_runtime.reserve_real_order=pause_after_reserve
        real_runtime.running.add(uid)
        asyncio.run(real_runtime._user_scan(uid))
        with Session() as session:
            rows=session.scalars(select(db.RealOrder).where(
                db.RealOrder.user_id==uid,
                db.RealOrder.account_id==account_id)).all()
            result.put(("ok",len(sent_calls),uid in real_runtime.running,
                        [(r.state,r.active_slot,r.contract_id,r.pnl) for r in rows]))
    except Exception as exc:
        result.put(("error",type(exc).__name__+":"+str(exc)[:250]))
    finally:
        db.SessionLocal=original_local
        real_runtime.running.discard(uid)
        engine.dispose()


def _revoke_while_preparing(dsn, started, finished, result):
    """Independent restart worker revokes persisted consent via shared DB."""
    try:
        engine=create_engine(dsn,pool_pre_ping=True)
        Session=sessionmaker(bind=engine,expire_on_commit=False)
        started.set()
        with Session() as session:
            count=revoke_stale_real_consent_after_restart(session)
        finished.set()
        result.put(("ok",count))
    except Exception as exc:
        result.put(("error",type(exc).__name__+":"+str(exc)[:200]))
    finally:
        if "engine" in locals():
            engine.dispose()


def _seed_pending_scan_account(session, name):
    user=db.User(deriv_account_id=name,selected_mode="demo",
                 stake="1",stop_win="0",stop_loss="0")
    session.add(user)
    session.flush()
    uid=user.id
    account_id="CRP004MIDSCAN"
    session.add(db.RealRobotConsent(user_id=uid,account_id=account_id,
                  max_stake="2",stop_loss="6"))
    session.add(db.AccountRiskSettings(
                  user_id=uid,account_mode="real",account_id=account_id,
                  stake="1",stop_win="0",stop_loss="6"))
    session.add(db.AutomationProfile(
                  user_id=uid,account_mode="real",
                  real_account_id=account_id,market="derived",strategy="trend",
                  symbols_json='["R_100"]',news_filter=False,
                  expiry_minutes=0,min_payout="88"))
    session.commit()
    return uid,account_id


def test_revoked_between_reservation_and_send_blocks_scanner_in_another_process():
    """Scanner process pauses after reservation; restart process revokes first.

    No broker method is ever allowed to run; all ledger records stay unchanged.
    """
    dsn=_safe_dsn()
    engine=create_engine(dsn,pool_pre_ping=True)
    db.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    ctx=multiprocessing.get_context("spawn")
    reserved=ctx.Event()
    continue_scan=ctx.Event()
    revoke_started=ctx.Event()
    revoke_finished=ctx.Event()
    scanner_result=ctx.Queue()
    revoke_result=ctx.Queue()
    scanner=revoker=None
    try:
        with Session() as s:
            uid,account_id=_seed_pending_scan_account(s,"V_P004_MIDSCAN")
            # Existing unresolved rows in OTHER accounts remain untouched.
            s.add(db.RealOrder(
                user_id=uid,account_id="CRP004UNRES",
                request_key="p004-unknown-prior",state="UNKNOWN",
                active_slot="CRP004UNRES",symbol="R_100",
                direction="PUT",duration_minutes=3,
                stake="1",currency="USD"))
            s.add(db.RealOrder(
                user_id=uid,account_id="CRP004CONF",
                request_key="p004-confirmed-prior",state="CONFIRMED",
                active_slot="CRP004CONF",contract_id="P004CONTRACT01",
                symbol="R_100",direction="CALL",
                duration_minutes=5,stake="1",currency="USD"))
            s.commit()
        scanner=ctx.Process(
            target=_pending_scan_process,
            args=(dsn,uid,account_id,reserved,continue_scan,scanner_result))
        scanner.start()
        assert reserved.wait(timeout=35), "Scanner never created synthetic reservation"
        with Session() as s:
            reserved_row=s.scalar(select(db.RealOrder).where(
                db.RealOrder.user_id==uid,
                db.RealOrder.account_id==account_id))
            assert reserved_row is not None
            assert reserved_row.state=="PENDING"
            assert reserved_row.active_slot==account_id
            assert reserved_row.contract_id is None
        revoker=ctx.Process(
            target=_revoke_while_preparing,
            args=(dsn,revoke_started,revoke_finished,revoke_result))
        revoker.start()
        assert revoke_started.wait(timeout=20)
        assert revoke_finished.wait(timeout=25), "Revocation must commit before scan resumes"
        assert revoke_result.get(timeout=20)[0]=="ok"
        with Session() as s:
            assert s.get(db.RealRobotConsent,uid).revoked_at is not None
        continue_scan.set()
        output=scanner_result.get(timeout=35)
        scanner.join(timeout=35)
        revoker.join(timeout=20)
        assert scanner.exitcode==0
        assert revoker.exitcode==0
        assert output==("ok",0,False,[("PENDING",account_id,None,None)]),output
        with Session() as s:
            rows=s.scalars(select(db.RealOrder).where(
                db.RealOrder.user_id==uid)).all()
            assert len(rows)==3
            state={o.account_id:(o.state,o.active_slot,o.contract_id,o.pnl) for o in rows}
            assert state[account_id]==("PENDING",account_id,None,None)
            assert state["CRP004UNRES"]==("UNKNOWN","CRP004UNRES",None,None)
            assert state["CRP004CONF"]==(
                "CONFIRMED","CRP004CONF","P004CONTRACT01",None)
    finally:
        continue_scan.set()
        for process in (scanner,revoker):
            if process is not None:
                process.join(timeout=3)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
        scanner_result.close()
        revoke_result.close()
        engine.dispose()


def _lock_holding_mock_sender(dsn,uid,account_id,entered,release,events):
    """Synthetic send marker only; holds a PostgreSQL row lock, no broker."""
    import time
    from app import restart_consistency
    engine=create_engine(dsn,pool_pre_ping=True)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    old=db.SessionLocal
    db.SessionLocal=Session
    try:
        with restart_consistency.locked_real_consent_for_send(uid,account_id) as allowed:
            entered.set()
            assert allowed
            assert release.wait(timeout=25)
            # Mere marker, not any network request or financial operation.
            events.put(("mock_sent",time.monotonic_ns()))
        events.put(("released",time.monotonic_ns()))
    except Exception as exc:
        events.put(("error",type(exc).__name__+":"+str(exc)[:160]))
    finally:
        db.SessionLocal=old
        engine.dispose()


def _lock_contending_revoker(dsn,started,events):
    import time
    engine=create_engine(dsn,pool_pre_ping=True)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    try:
        started.set()
        with Session() as session:
            count=revoke_stale_real_consent_after_restart(session)
        events.put(("revoked",time.monotonic_ns(),count))
    except Exception as exc:
        events.put(("error",type(exc).__name__+":"+str(exc)[:160]))
    finally:
        engine.dispose()


def test_postgresql_consent_lock_serializes_outbound_boundary_and_revocation():
    """Revocation cannot commit between an authorized check and mock send."""
    import queue
    dsn=_safe_dsn()
    engine=create_engine(dsn,pool_pre_ping=True)
    db.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    ctx=multiprocessing.get_context("spawn")
    entered,release,revoker_started=ctx.Event(),ctx.Event(),ctx.Event()
    events=ctx.Queue()
    holder=revoker=None
    try:
        with Session() as s:
            uid,account_id=_seed_pending_scan_account(s,"V_P004_ROW_LOCK")
        holder=ctx.Process(
            target=_lock_holding_mock_sender,
            args=(dsn,uid,account_id,entered,release,events))
        holder.start()
        assert entered.wait(timeout=35)
        revoker=ctx.Process(
            target=_lock_contending_revoker,
            args=(dsn,revoker_started,events))
        revoker.start()
        assert revoker_started.wait(timeout=25)
        # The revocation transaction cannot commit while the consent row
        # is locked by the final send guard.
        with pytest.raises(queue.Empty):
            events.get(timeout=0.35)
        release.set()
        observed=[events.get(timeout=35) for _ in range(3)]
        holder.join(timeout=25)
        revoker.join(timeout=25)
        assert holder.exitcode==revoker.exitcode==0
        labels=[item[0] for item in observed]
        assert set(labels)=={"mock_sent","released","revoked"},observed
        stamps={item[0]:item[1] for item in observed}
        assert stamps["mock_sent"]<stamps["revoked"]
        assert stamps["released"]<=stamps["revoked"]
        with Session() as session:
            assert session.get(db.RealRobotConsent,uid).revoked_at is not None
            assert not session.scalars(select(db.RealOrder).where(
                db.RealOrder.user_id==uid)).all()
    finally:
        release.set()
        for p in (holder,revoker):
            if p is not None:
                p.join(timeout=3)
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=5)
        events.close()
        engine.dispose()

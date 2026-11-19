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
        with Session() as first:
            assert revoke_stale_real_consent_after_restart(first)==1
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

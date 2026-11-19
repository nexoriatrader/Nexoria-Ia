"""P0-05: cross-process read-only status on disposable localhost PostgreSQL.

Real administrative flags remain OFF in CI. The 'gate=True' argument below
is a *pure synthetic unit scenario*, not an environment change or permission
to start any worker. No network, tokens or broker calls occur.
"""
import multiprocessing
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app import db
from app.status_contract import contract_summary, evaluate_user_status


def _dsn():
    if os.getenv("NEXORA_P0_EPHEMERAL_PG")!="yes":
        pytest.skip("P0-05 PostgreSQL verification is CI-only")
    raw=os.environ["NEXORA_P0_TEST_DATABASE_URL"]
    url=make_url(raw)
    if not (url.drivername=="postgresql+psycopg"
            and url.host in ("127.0.0.1","localhost") and url.port==5432
            and url.database=="nexora_p0_ci" and url.username=="p0_ci"):
        pytest.fail("Non-disposable database refused")
    assert os.getenv("NEXORA_REAL_AUTOPILOT_ENABLED","false")=="false"
    assert os.getenv("NEXORA_REAL_RELEASE_APPROVED","false")=="false"
    return raw


class MockLocalWorker:
    def done(self):
        return False


def _independent_status(dsn,uid,local_attested,results):
    engine=create_engine(dsn,pool_pre_ping=True)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    try:
        with Session() as s:
            u=s.get(db.User,uid)
            profile=s.get(db.AutomationProfile,uid)
            result=evaluate_user_status(
                s,u,profile=profile,
                local_demo=False,local_real=local_attested,
                demo_scheduler=None,
                real_scheduler=MockLocalWorker() if local_attested else None,
                demo_gate=False,real_gate=True,real_feature_gate=True,
                real_release_approved=True
            )
            results.put(("ok",result["running_now"],
                         result["running_evidence"],result["contracts"]["counts"]))
    except Exception as exc:
        results.put(("error",type(exc).__name__+":"+str(exc)[:130]))
    finally:
        engine.dispose()


def _child_read(dsn,uid,local):
    ctx=multiprocessing.get_context("spawn")
    result=ctx.Queue()
    p=ctx.Process(target=_independent_status,args=(dsn,uid,local,result))
    try:
        p.start()
        item=result.get(timeout=30)
        p.join(timeout=25)
        assert p.exitcode==0,item
        assert item[0]=="ok",item
        return item
    finally:
        if p.is_alive():
            p.terminate()
            p.join(timeout=5)
        result.close()


def test_two_processes_do_not_invent_global_running_or_settle_confirmed():
    dsn=_dsn()
    engine=create_engine(dsn,pool_pre_ping=True)
    db.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    try:
        with Session() as s:
            u=db.User(deriv_account_id="V_P005_POSTGRES",selected_mode="demo")
            s.add(u)
            s.flush()
            uid=u.id
            s.add(db.AutomationProfile(user_id=uid,account_mode="real",
                                       real_account_id="CRP005POSTGRES"))
            s.add(db.RealRobotConsent(user_id=uid,account_id="CRP005POSTGRES",
                                      max_stake="1",stop_loss="5"))
            s.add(db.RealOrder(user_id=uid,account_id="CRP005POSTGRES",
                               request_key="p005-confirmed-open",
                               active_slot="CRP005POSTGRES",state="CONFIRMED",
                               contract_id="P005SYNTHETIC",
                               symbol="R_100",direction="CALL",
                               stake="1",currency="USD",duration_minutes=5))
            s.commit()
        # Local task simulation in one process must not turn another process'
        # missing liveness into False or True. It is UNKNOWN.
        worker_a=_child_read(dsn,uid,True)
        worker_b=_child_read(dsn,uid,False)
        assert worker_a[1] is True
        assert worker_b[1] is None
        assert worker_a[3]["CONFIRMED"]==worker_b[3]["CONFIRMED"]==1
        assert worker_a[3]["SETTLED"]==worker_b[3]["SETTLED"]==0
        with Session() as s:
            summary=contract_summary(s,uid,account_mode="real",
                                     account_id="CRP005POSTGRES")
            assert summary["confirmed_open_count"]==1
            assert summary["open_unsettled"]==1
            assert summary["settled_count"]==0
            row=s.query(db.RealOrder).filter_by(user_id=uid).one()
            assert row.state=="CONFIRMED"
            assert row.active_slot=="CRP005POSTGRES"
            assert row.pnl is None
    finally:
        engine.dispose()


def test_other_process_revocation_is_reflected_without_worker_side_effects():
    dsn=_dsn()
    engine=create_engine(dsn,pool_pre_ping=True)
    db.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    try:
        with Session() as s:
            user=db.User(deriv_account_id="V_P005_PG_REVOKED",
                         selected_mode="demo")
            s.add(user)
            s.flush()
            uid=user.id
            s.add(db.AutomationProfile(user_id=uid,account_mode="real",
                                       real_account_id="CRP005REVOKED"))
            s.add(db.RealRobotConsent(user_id=uid,account_id="CRP005REVOKED",
                                      max_stake="1",stop_loss="5"))
            s.commit()
        assert _child_read(dsn,uid,False)[1] is None
        with Session() as other_process:
            consent=other_process.get(db.RealRobotConsent,uid)
            consent.revoked_at=db.utcnow()
            other_process.commit()
        assert _child_read(dsn,uid,True)[1] is False
        assert _child_read(dsn,uid,False)[1] is False
    finally:
        engine.dispose()

"""P0-03: cross-PROCESS uniqueness and stale-writer tests, ephemeral Postgres ONLY.

No Deriv, credentials, network trading, Supabase or Render. Processes use
separate SQLAlchemy engines and synthetic database records. The strict local
DSN guard prevents accidental connection to shared or production databases.
"""
from __future__ import annotations

import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import db


def _safe_dsn():
    if os.getenv("NEXORA_P0_EPHEMERAL_PG") != "yes":
        pytest.skip("Requires disposable PostgreSQL in CI")
    dsn=os.environ["NEXORA_P0_TEST_DATABASE_URL"]
    url=make_url(dsn)
    if not (
        url.drivername=="postgresql+psycopg"
        and url.host in ("127.0.0.1","localhost")
        and url.port==5432 and url.database=="nexora_p0_ci"
        and url.username=="p0_ci"
    ):
        pytest.fail("Refusing non-disposable database destination")
    return dsn


def _writer(dsn,kind,user_id,scenario,worker_number,go,ready,out):
    """Independent process; only synthetic SQL INSERT, no API imports."""
    engine=create_engine(dsn,pool_pre_ping=True)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    try:
        ready.put(worker_number)
        if not go.wait(timeout=30):
            out.put(("error","start timeout"))
            return
        with Session() as s:
            try:
                if kind=="demo":
                    order=db.DemoOrder(
                        user_id=user_id,request_key=f"p3demo-{scenario}-{worker_number}",
                        active_slot=user_id,state="PENDING",symbol="R_100",
                        direction="CALL",duration_minutes=5,stake="1")
                else:
                    account=(f"CRPG3{scenario}" if scenario!="contract"
                             else f"CRPG3C{worker_number}")
                    key=(f"p3-{scenario}-duplicate" if scenario=="request"
                         else f"p3-{scenario}-write-{worker_number}")
                    slot=(account if scenario=="slot" else None)
                    cid=("606060606" if scenario=="contract"
                         else f"6{worker_number}0606060")
                    order=db.RealOrder(
                        user_id=user_id,account_id=account,request_key=key,
                        active_slot=slot,state="PENDING" if scenario=="slot" else "SETTLED",
                        symbol="R_100",direction="CALL",duration_minutes=5,
                        stake="1",currency="USD",contract_id=None if scenario=="slot" else cid,
                        pnl=None if scenario=="slot" else "0")
                s.add(order)
                s.commit()
                out.put(("ok",worker_number))
            except IntegrityError:
                s.rollback()
                out.put(("rejected",worker_number))
    except Exception as exc:
        out.put(("error",type(exc).__name__+":"+str(exc)[:120]))
    finally:
        engine.dispose()


def _settle_writer(dsn,order_id,profit,go,ready,out):
    """Independent process simulates two delayed OFFICIAL status callbacks."""
    from app.real_ledger import settle_real_order, RealOrderGuardError
    engine=create_engine(dsn,pool_pre_ping=True)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    try:
        with Session() as s:
            row=s.get(db.RealOrder,order_id)
            if row is None:
                out.put(("error","missing test row"))
                return
            ready.put(profit)
            if not go.wait(timeout=30):
                out.put(("error","start timeout"))
                return
            result=settle_real_order(s,row,{
                "contract_id":"808080808","is_sold":1,
                "profit":profit,"status":"synthetic"
            })
            out.put(("settled" if result else "already_closed",profit))
    except Exception as exc:
        out.put(("error",type(exc).__name__+":"+str(exc)[:120]))
    finally:
        engine.dispose()


def _parallel(ctx,target,arguments):
    ready,out=ctx.Queue(),ctx.Queue()
    go=ctx.Event()
    processes=[ctx.Process(target=target,args=(*args,go,ready,out)) for args in arguments]
    try:
        for p in processes:
            p.start()
        for _ in processes:
            ready.get(timeout=45)
        go.set()
        results=[out.get(timeout=45) for _ in processes]
        for p in processes:
            p.join(timeout=45)
            assert p.exitcode==0, f"Independent process failed: {p.exitcode}"
        return results
    finally:
        go.set()
        for p in processes:
            if p.is_alive():
                p.terminate()
                p.join(timeout=5)
        ready.close()
        out.close()


@pytest.mark.parametrize("kind,scenario",[
    ("real","slot"),
    ("real","request"),
    ("real","contract"),
    ("demo","slot"),
])
def test_cross_process_uniqueness_is_enforced_by_postgres(kind,scenario):
    dsn=_safe_dsn()
    engine=create_engine(dsn,pool_pre_ping=True)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    try:
        db.Base.metadata.create_all(engine)
        with Session() as s:
            u=db.User(deriv_account_id=f"VP3{scenario}{kind}",selected_mode="demo")
            s.add(u);s.commit()
            uid=u.id
        ctx=multiprocessing.get_context("spawn")
        result=_parallel(ctx,_writer,[(dsn,kind,uid,scenario,i) for i in (1,2)])
        assert sorted(x[0] for x in result)==["ok","rejected"],result
        with Session() as s:
            if kind=="demo":
                found=s.scalars(select(db.DemoOrder).where(
                    db.DemoOrder.user_id==uid)).all()
            else:
                found=s.scalars(select(db.RealOrder).where(
                    db.RealOrder.user_id==uid)).all()
            assert len(found)==1
        # The rejected process rolled back and did not leave a phantom row.
    finally:
        engine.dispose()


def test_stale_settlement_from_independent_process_cannot_overwrite_pnl():
    dsn=_safe_dsn()
    engine=create_engine(dsn,pool_pre_ping=True)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    try:
        db.Base.metadata.create_all(engine)
        with Session() as s:
            u=db.User(deriv_account_id="VP3SETTLEMENT",selected_mode="demo")
            s.add(u);s.flush()
            row=db.RealOrder(user_id=u.id,account_id="CRPG3SETTLE",
                request_key="p3-settlement-idempotent",active_slot="CRPG3SETTLE",
                state="CONFIRMED",contract_id="808080808",symbol="R_100",
                direction="CALL",duration_minutes=5,stake="1",currency="USD")
            s.add(row);s.commit()
            oid=row.id
        ctx=multiprocessing.get_context("spawn")
        result=_parallel(ctx,_settle_writer,[
            (dsn,oid,"0.75"),(dsn,oid,"-0.99")
        ])
        assert sorted(x[0] for x in result)==["already_closed","settled"],result
        with Session() as s:
            row=s.get(db.RealOrder,oid)
            assert row.state=="SETTLED" and row.active_slot is None
            assert row.pnl in ("0.75","-0.99")
            assert s.scalars(select(db.RealOrder).where(
                db.RealOrder.contract_id=="808080808")).all().__len__()==1
    finally:
        engine.dispose()

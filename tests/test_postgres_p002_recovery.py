"""P0-02 PostgreSQL-only recovery check on ephemeral GitHub CI database.

No Supabase, Render, Deriv, purchase API or real money. Rejects remote DSNs.
"""
import os
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app import db
from app.real_recovery import account_recovery_summary, promote_stale_pending_to_unknown


def _test_engine():
    if os.getenv("NEXORA_P0_EPHEMERAL_PG") != "yes":
        pytest.skip("Disposable PostgreSQL check runs on isolated CI only")
    url = make_url(os.environ["NEXORA_P0_TEST_DATABASE_URL"])
    if not (url.drivername == "postgresql+psycopg"
            and url.host in ("127.0.0.1", "localhost")
            and url.port == 5432 and url.database == "nexora_p0_ci"
            and url.username == "p0_ci"):
        pytest.fail("Refusing to touch non-ephemeral PostgreSQL")
    return create_engine(url, pool_pre_ping=True)


def test_recovery_is_transactional_and_idempotent_in_disposable_postgres():
    engine = _test_engine()
    try:
        db.Base.metadata.create_all(engine)
        assert "real_orders" in inspect(engine).get_table_names()
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        with Session() as s:
            user = db.User(deriv_account_id="V_POSTGRES_P002",selected_mode="demo")
            s.add(user);s.flush()
            uid = user.id
            old_time = db.utcnow() - timedelta(minutes=45)
            recent_time = db.utcnow() - timedelta(minutes=1)
            stale = db.RealOrder(
                user_id=uid,account_id="CRPOST1",request_key="pg-stale-12345",
                active_slot="CRPOST1",state="PENDING",symbol="R_100",
                direction="CALL",duration_minutes=5,stake="1",currency="USD",
                created_at=old_time,updated_at=old_time)
            recent = db.RealOrder(
                user_id=uid,account_id="CRPOST2",request_key="pg-fresh-12345",
                active_slot="CRPOST2",state="PENDING",symbol="R_100",
                direction="PUT",duration_minutes=5,stake="1",currency="USD",
                created_at=recent_time,updated_at=recent_time)
            s.add_all((stale,recent))
            s.commit()
            stale_id,recent_id=stale.id,recent.id
        with Session() as first:
            assert promote_stale_pending_to_unknown(first)==[stale_id]
        with Session() as second:
            assert promote_stale_pending_to_unknown(second)==[]
            old=second.get(db.RealOrder,stale_id)
            fresh=second.get(db.RealOrder,recent_id)
            assert old.state=="UNKNOWN"
            assert old.active_slot=="CRPOST1"
            assert old.contract_id is None and old.pnl is None
            assert old.request_key=="pg-stale-12345"
            assert fresh.state=="PENDING" and fresh.active_slot=="CRPOST2"
            assert account_recovery_summary(second,uid,"CRPOST1")["blocked"]
            assert account_recovery_summary(second,uid,"CRPOST2")["blocked"]
    finally:
        engine.dispose()

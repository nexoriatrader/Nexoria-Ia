"""Run ONLY against a disposable localhost PostgreSQL service in GitHub Actions.

Proves forward migration, additive legacy preservation, backout and restoration.
Never connects to Supabase/Render/Deriv. The safety guard rejects remote DSNs.
"""
import os

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app import db


def temporary_postgres():
    if os.getenv("NEXORA_P0_EPHEMERAL_PG") != "yes":
        pytest.skip("Disposable PostgreSQL test is CI-only")
    dsn = os.environ["NEXORA_P0_TEST_DATABASE_URL"]
    parsed = make_url(dsn)
    if not (parsed.drivername == "postgresql+psycopg"
            and parsed.host in ("localhost", "127.0.0.1")
            and parsed.port == 5432
            and parsed.database == "nexora_p0_ci"
            and parsed.username == "p0_ci"):
        pytest.fail("Refusing any non-disposable PostgreSQL destination")
    return create_engine(dsn, pool_pre_ping=True)


def test_real_postgresql_legacy_migration_backout_and_restore(monkeypatch):
    engine = temporary_postgres()
    try:
        old_tables = [table for table in db.Base.metadata.sorted_tables
                      if table.name != "account_risk_settings"]
        db.Base.metadata.create_all(engine, tables=old_tables)
        assert "account_risk_settings" not in inspect(engine).get_table_names()
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as session:
            user = db.User(deriv_account_id="V_EPH_PG", selected_mode="demo",
                           stake="3.50", stop_win="15.00", stop_loss="9.00")
            session.add(user)
            session.flush()
            uid = user.id
            session.add(db.RobotPlan(user_id=uid, gale_count=2,
                                     requested_martingale=True,
                                     requested_multiplier="1.50", cycles_limit=0))
            session.add(db.RealRobotConsent(
                user_id=uid,account_id="CR_EPH_PG",max_stake="4.00",stop_loss="7.00"))
            session.commit()

        # F01 safety contract: runtime must NEVER silently migrate PostgreSQL.
        # The legacy table is provisioned ONLY by this disposable test.
        monkeypatch.setattr(db, "engine", engine)
        with pytest.raises(RuntimeError,match="migração formal obrigatória"):
            db.init_db()
        assert "account_risk_settings" not in inspect(engine).get_table_names()
        db.AccountRiskSettings.__table__.create(engine,checkfirst=True)
        assert "account_risk_settings" in inspect(engine).get_table_names()
        columns = {c["name"] for c in inspect(engine).get_columns("account_risk_settings")}
        assert {"user_id", "account_mode", "account_id", "stake",
                "stop_win", "stop_loss", "revision"} <= columns

        with factory() as session:
            session.add(db.AccountRiskSettings(
                user_id=uid, account_mode="real", account_id="CR_EPH_PG",
                stake="4.00", stop_win="10.00", stop_loss="7.00", revision=1))
            session.add(db.AccountRiskSettings(
                user_id=uid, account_mode="demo", account_id="V_EPH_PG",
                stake="3.50", stop_win="15.00", stop_loss="9.00", revision=1))
            session.commit()
            backup = [{
                "user_id": row.user_id, "account_mode": row.account_mode,
                "account_id": row.account_id, "stake": row.stake,
                "stop_win": row.stop_win, "stop_loss": row.stop_loss,
                "revision": row.revision,
            } for row in session.query(db.AccountRiskSettings).all()]
            assert len(backup) == 2

        # Explicit schema rollback after synthetic export, legacy tables untouched.
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE account_risk_settings"))
        assert "account_risk_settings" not in inspect(engine).get_table_names()
        with factory() as session:
            assert session.get(db.User, uid).stake == "3.50"
            assert session.get(db.User, uid).stop_loss == "9.00"
            assert session.get(db.RobotPlan, uid).gale_count == 2
            assert session.get(db.RealRobotConsent, uid).stop_loss == "7.00"

        # Explicit disposable-schema restore, NEVER an application startup DDL.
        with pytest.raises(RuntimeError,match="migração formal obrigatória"):
            db.init_db()
        db.AccountRiskSettings.__table__.create(engine,checkfirst=True)
        with factory() as session:
            for entry in backup:
                session.add(db.AccountRiskSettings(**entry))
            session.commit()
            restored = session.query(db.AccountRiskSettings).all()
            assert len(restored) == 2
            assert {(x.account_mode,x.account_id,x.stop_loss) for x in restored} == {
                ("real","CR_EPH_PG","7.00"), ("demo","V_EPH_PG","9.00")}
            assert session.get(db.User,uid).stop_loss == "9.00"
    finally:
        engine.dispose()

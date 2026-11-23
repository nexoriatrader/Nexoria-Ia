"""Disposable PostgreSQL formal migration and read-only startup contract.

CI only. Never connect to Render, production, Deriv, or any financial service.
"""
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import db


PG = os.getenv("NEXORA_CONSOLIDATION_TEST_DATABASE_URL", "")
EXPECTED = "postgresql+psycopg://research:disposable@127.0.0.1:5432/nexora_public_test"
pytestmark = pytest.mark.skipif(
    PG != EXPECTED, reason="Only the exact disposable CI PostgreSQL endpoint is allowed",
)
ROOT = Path(__file__).resolve().parents[1] / "migrations"


def apply_migration(engine, path):
    sql = (ROOT / path).read_text(encoding="utf-8")
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(sql, prepare=False)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def test_formal_wizard_migration_persists_and_startup_performs_zero_ddl(monkeypatch):
    """The old schema upgrades only through a formal, repeatable migration."""
    engine = create_engine(PG, pool_pre_ping=True)
    wizard_tables = {"robot_usage_modes", "robot_manual_signals"}
    try:
        # Simulate pre-feature tables in a fresh and disposable test database.
        # Crucially, create_all cannot install the new wizard tables in this test.
        db.Base.metadata.create_all(
            engine,
            tables=[table for table in db.Base.metadata.sorted_tables
                    if table.name not in wizard_tables],
        )
        with engine.begin() as con:
            con.exec_driver_sql("""
                CREATE TABLE robot_usage_modes (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    mode VARCHAR(16) NOT NULL DEFAULT 'intelligent',
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    CONSTRAINT robot_usage_modes_valid_mode
                        CHECK (mode IN ('intelligent','manual'))
                )
            """)
        apply_migration(engine, "F01_GLOBAL_FENCING_V1_POSTGRES.sql")
        with engine.connect() as con:
            with pytest.raises(RuntimeError, match="formal migration|required"):
                db.verify_postgresql_schema_read_only(con)

        apply_migration(engine, "20261123_robot_modes_manual_signals_homologation.sql")
        # The formal migration must be idempotent and non-destructive.
        apply_migration(engine, "20261123_robot_modes_manual_signals_homologation.sql")
        inspector = inspect(engine)
        assert wizard_tables.issubset(set(inspector.get_table_names()))
        assert "preferences_json" in {
            col["name"] for col in inspector.get_columns("robot_usage_modes")}
        assert "uq_robot_manual_signals_user_slot" in {
            idx["name"] for idx in inspector.get_indexes("robot_manual_signals")}
        with engine.connect() as con:
            db.verify_postgresql_schema_read_only(con)

        Session = sessionmaker(bind=engine, expire_on_commit=False)
        with Session() as sess:
            user = db.User(deriv_account_id="VPOSTGRES_DRAFT", selected_mode="demo")
            sess.add(user)
            sess.flush()
            uid = user.id
            sess.add(db.RobotUsageMode(
                user_id=uid, mode="manual",
                preferences_json='{"recovery":"none","cycles":0,"martingale_attempts":0}',
            ))
            from datetime import datetime, timedelta, timezone
            when = datetime.now(timezone.utc) + timedelta(days=1)
            sess.add(db.RobotManualSignal(
                user_id=uid, symbol="1HZ100V", direction="CALL",
                expiry_minutes=5, scheduled_for=when,
            ))
            sess.commit()
        apply_migration(engine, "20261123_robot_modes_manual_signals_homologation.sql")
        with Session() as sess:
            assert sess.get(db.RobotUsageMode, uid).mode == "manual"
            assert sess.get(db.RobotUsageMode, uid).preferences_json is not None
            assert sess.query(db.RobotManualSignal).filter_by(user_id=uid).count() == 1
            sess.add(db.RobotManualSignal(
                user_id=uid, symbol="1HZ100V", direction="CALL",
                expiry_minutes=5, scheduled_for=when,
            ))
            with pytest.raises(IntegrityError):
                sess.commit()
            sess.rollback()

        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith((
                "CREATE ", "ALTER ", "DROP ", "INSERT ", "UPDATE ", "DELETE ", "TRUNCATE ",
            )):
                statements.append(statement[:120])
        monkeypatch.setattr(db, "engine", engine)
        event.listen(engine, "before_cursor_execute", capture)
        try:
            db.init_db()
            assert statements == [], "PostgreSQL app startup must be read-only"
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    finally:
        # Isolated CI DB, not a production object or database.
        with engine.begin() as con:
            for table in ("real_authorization_fences", "real_global_authorization_fences",
                          "nexora_schema_revisions"):
                con.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
        db.Base.metadata.drop_all(engine)
        engine.dispose()

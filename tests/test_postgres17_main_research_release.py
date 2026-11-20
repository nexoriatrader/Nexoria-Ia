"""PG 17.11 disposable-only compatibility of unchanged main schema and research.

Never use an external DB or database migration. This deliberately does NOT
certify the F01/I04 migrations, which live in separate stacked PRs.
"""
import os,uuid
import pytest
from sqlalchemy import create_engine,text,inspect
from sqlalchemy.engine import make_url
from app import db

def test_postgres17_11_main_schema_and_readonly_modules(monkeypatch):
    if os.getenv("NEXORA_P0_EPHEMERAL_PG")!="yes":
        pytest.skip("Postgres test runs only in isolated CI")
    raw=os.environ["NEXORA_P0_TEST_DATABASE_URL"]
    uri=make_url(raw)
    if not (uri.drivername=="postgresql+psycopg" 
        and uri.host in ("localhost","127.0.0.1") and uri.port==5432
        and uri.database=="nexora_p0_ci" and uri.username=="p0_ci"):
        pytest.fail("Refusing non-disposable database")
    admin=create_engine(raw,pool_pre_ping=True)
    schema="research17_"+uuid.uuid4().hex[:12]
    with admin.begin() as conn:
        assert conn.execute(text("SHOW server_version")).scalar().startswith("17.11")
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine=create_engine(raw,connect_args={"options":f"-c search_path={schema}"})
    try:
        db.Base.metadata.create_all(engine)  # local disposable fixture ONLY
        with engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('users')")).scalar() is not None
            assert conn.execute(text("SELECT to_regclass('real_orders')")).scalar() is not None
            assert conn.execute(text("SELECT to_regclass('intelligence_knowledge')")).scalar() is None
            assert conn.execute(text("SELECT to_regclass('nexora_schema_revisions')")).scalar() is None
            assert engine.dialect.name=="postgresql"
            assert set(inspect(conn).get_table_names())==set(db.Base.metadata.tables)
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()

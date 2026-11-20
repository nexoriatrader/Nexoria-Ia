"""F01: no application PostgreSQL DDL; formal migration required.

All DDL below is EXPLICIT ephemeral-test setup/teardown, never app.init_db.
The DSN is restricted to the disposable PostgreSQL CI service.
"""
import os
import re
import uuid

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from app import db


def _safe_url():
    if os.getenv('NEXORA_P0_EPHEMERAL_PG')!='yes':
        pytest.skip('F01 schema guard only executes on disposable PostgreSQL')
    url=make_url(os.environ['NEXORA_P0_TEST_DATABASE_URL'])
    if not (url.drivername=='postgresql+psycopg'
            and url.host in ('localhost','127.0.0.1') and url.port==5432
            and url.database=='nexora_p0_ci' and url.username=='p0_ci'):
        pytest.fail('Non-disposable PostgreSQL rejected')
    return str(url)


def test_postgresql_init_db_is_strictly_read_only_and_requires_formal_revision(monkeypatch):
    dsn=_safe_url()
    admin=create_engine(dsn,pool_pre_ping=True)
    schema='f01_guard_'+uuid.uuid4().hex[:10]
    # Explicit test-schema setup: no production/Render database.
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated=create_engine(dsn,pool_pre_ping=True,
                           connect_args={'options':f'-c search_path={schema}'})
    statements=[]
    def inspect_statement(conn,cursor,statement,parameters,context,executemany):
        statements.append(statement)
    try:
        monkeypatch.setattr(db,'engine',isolated)
        event.listen(isolated,'before_cursor_execute',inspect_statement)
        with pytest.raises(RuntimeError,match='migração formal obrigatória'):
            db.init_db()
        assert not any(re.match(r'^\s*(CREATE|ALTER|DROP|TRUNCATE|REINDEX)\b',
                                x,re.I) for x in statements),statements

        # Explicit setup permitted ONLY in the disposable test schema:
        event.remove(isolated,'before_cursor_execute',inspect_statement)
        db.Base.metadata.create_all(isolated)
        event.listen(isolated,'before_cursor_execute',inspect_statement)
        statements.clear()
        with pytest.raises(RuntimeError,match='migração formal obrigatória'):
            db.init_db()
        assert not any(re.match(r'^\s*(CREATE|ALTER|DROP|TRUNCATE|REINDEX)\b',
                                x,re.I) for x in statements),statements

        # Simulate the separately authorized migration in a disposable schema.
        event.remove(isolated,'before_cursor_execute',inspect_statement)
        with isolated.begin() as conn:
            conn.execute(text('CREATE TABLE nexora_schema_revisions ('
                'revision VARCHAR(80) PRIMARY KEY, '
                'applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP)'))
            conn.execute(text('INSERT INTO nexora_schema_revisions(revision) '
                'VALUES (:v)'),{'v':db.F01_REQUIRED_REVISION})
        event.listen(isolated,'before_cursor_execute',inspect_statement)
        statements.clear()
        db.init_db()
        assert not any(re.match(r'^\s*(CREATE|ALTER|DROP|TRUNCATE|REINDEX|INSERT|UPDATE|DELETE)\b',
                                x,re.I) for x in statements),statements

        # Missing singleton epoch fails closed even when migration is stamped.
        event.remove(isolated,'before_cursor_execute',inspect_statement)
        with isolated.begin() as conn:
            conn.execute(text('DELETE FROM real_global_authorization_fences WHERE id=1'))
        event.listen(isolated,'before_cursor_execute',inspect_statement)
        statements.clear()
        with pytest.raises(RuntimeError,match='Geração global F01 ausente'):
            db.init_db()
        assert not any(re.match(r'^\s*(CREATE|ALTER|DROP|TRUNCATE|REINDEX|INSERT|UPDATE|DELETE)\b',
                                x,re.I) for x in statements),statements

        # Version marker does not silently bootstrap any missing schema.
        with pytest.raises(RuntimeError,match='Geração global F01 ausente'):
            db.init_db()
    finally:
        try:
            event.remove(isolated,'before_cursor_execute',inspect_statement)
        except Exception:
            pass
        isolated.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_postgresql_migration_version_missing_blocks_all_automatic_ddl(monkeypatch):
    dsn=_safe_url()
    admin=create_engine(dsn,pool_pre_ping=True)
    schema='f01_empty_'+uuid.uuid4().hex[:10]
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated=create_engine(dsn,pool_pre_ping=True,
                           connect_args={'options':f'-c search_path={schema}'})
    try:
        monkeypatch.setattr(db,'engine',isolated)
        for _ in range(2):
            with pytest.raises(RuntimeError,match='migração formal obrigatória'):
                db.init_db()
            with isolated.connect() as conn:
                found=conn.execute(text(
                    'SELECT tablename FROM pg_tables WHERE schemaname=:name'
                ),{'name':schema}).all()
                assert found==[],found
    finally:
        isolated.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()

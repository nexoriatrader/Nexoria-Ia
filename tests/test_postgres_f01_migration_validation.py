"""F01 formal migration contract tests, isolated PostgreSQL 16 ONLY.

No production database or broker access. Every test creates and drops a schema
inside the exact disposable localhost CI instance, including error cases.
"""
from pathlib import Path
import os
import uuid

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import db

MIGRATION = (Path(__file__).resolve().parents[1] / 'migrations'
             / 'F01_GLOBAL_FENCING_V1_POSTGRES.sql').read_text(encoding='utf-8')


def _safe_dsn():
    if os.getenv('NEXORA_P0_EPHEMERAL_PG') != 'yes':
        pytest.skip('Only allowed on disposable PostgreSQL 16 in CI')
    raw = os.environ['NEXORA_P0_TEST_DATABASE_URL']
    url = make_url(raw)
    if not (url.drivername == 'postgresql+psycopg'
            and url.host in ('127.0.0.1', 'localhost')
            and url.port == 5432 and url.database == 'nexora_p0_ci'
            and url.username == 'p0_ci'):
        pytest.fail('Refusing non-disposable PostgreSQL DSN')
    assert os.getenv('NEXORA_REAL_AUTOPILOT_ENABLED', 'false') == 'false'
    assert os.getenv('NEXORA_REAL_RELEASE_APPROVED', 'false') == 'false'
    return raw


@pytest.fixture
def isolated_f01(monkeypatch):
    dsn = _safe_dsn()
    admin = create_engine(dsn, pool_pre_ping=True)
    schema = 'f01_migration_' + uuid.uuid4().hex[:12]
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = create_engine(
        dsn, pool_pre_ping=True,
        connect_args={'options': f'-c search_path={schema}'})
    monkeypatch.setattr(db, 'engine', isolated)
    try:
        yield dsn, schema, isolated
    finally:
        isolated.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _apply(dsn, schema):
    # psycopg simple-query protocol accepts this entire transaction including
    # PL/pgSQL DO blocks. It never interpolates external/untrusted SQL.
    pg_dsn = dsn.replace('postgresql+psycopg://', 'postgresql://', 1)
    with psycopg.connect(pg_dsn, autocommit=True,
                         options=f'-c search_path={schema}') as conn:
        conn.execute(MIGRATION, prepare=False)


def _stamped(engine):
    with engine.connect() as conn:
        if conn.execute(text("SELECT to_regclass('nexora_schema_revisions')")).scalar() is None:
            return False
        return conn.execute(text(
            "SELECT count(*) FROM nexora_schema_revisions "
            "WHERE revision = 'f01_global_fencing_v1'")).scalar_one() == 1


def test_f01_migration_creates_missing_tables_and_is_idempotent(isolated_f01):
    dsn, schema, engine = isolated_f01
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE users (id INTEGER PRIMARY KEY)'))
    assert not _stamped(engine)
    _apply(dsn, schema)
    assert _stamped(engine)
    with engine.begin() as conn:
        assert conn.execute(text(
            'SELECT id, generation FROM real_global_authorization_fences')).all() == [(1, 0)]
        conn.execute(text(
            'UPDATE real_global_authorization_fences SET generation=7 WHERE id=1'))
        conn.execute(text('INSERT INTO users(id) VALUES (42)'))
        conn.execute(text('INSERT INTO real_authorization_fences(user_id) VALUES (42)'))
    _apply(dsn, schema)  # Re-running must not reset fencing generations.
    with engine.connect() as conn:
        assert conn.execute(text(
            'SELECT generation FROM real_global_authorization_fences WHERE id=1')).scalar_one() == 7
        assert conn.execute(text(
            'SELECT generation FROM real_authorization_fences WHERE user_id=42')).scalar_one() == 0
        assert conn.execute(text(
            'SELECT count(*) FROM nexora_schema_revisions '
            "WHERE revision='f01_global_fencing_v1'")).scalar_one() == 1


def test_f01_migration_valid_existing_schema_then_startup_read_only(isolated_f01):
    dsn, schema, engine = isolated_f01
    db.Base.metadata.create_all(engine)  # Explicit disposable fixture setup.
    with pytest.raises(RuntimeError, match='migração formal obrigatória'):
        db.init_db()
    _apply(dsn, schema)
    db.init_db()
    _apply(dsn, schema)
    db.init_db()
    assert _stamped(engine)


# Each defect exists *before* migration; existing objects must never be
# accepted merely because CREATE TABLE IF NOT EXISTS returned a notice.
_DEFECTS = {
    'missing_column': 'ALTER TABLE real_authorization_fences DROP COLUMN updated_at',
    'incorrect_type': 'ALTER TABLE real_authorization_fences ALTER COLUMN generation TYPE BIGINT',
    'incorrect_timestamp_type': (
        'ALTER TABLE real_global_authorization_fences '
        'ALTER COLUMN updated_at TYPE TIMESTAMP WITHOUT TIME ZONE'),
    'missing_fk': (
        'ALTER TABLE real_authorization_fences '
        'DROP CONSTRAINT real_authorization_fences_user_id_fkey'),
    'missing_pk': (
        'ALTER TABLE real_authorization_fences '
        'DROP CONSTRAINT real_authorization_fences_pkey'),
    'missing_check': (
        'ALTER TABLE real_authorization_fences '
        'DROP CONSTRAINT real_authorization_fences_generation_check'),
    'unvalidated_check': (
        'ALTER TABLE real_authorization_fences '
        'DROP CONSTRAINT real_authorization_fences_generation_check; '
        'ALTER TABLE real_authorization_fences ADD CONSTRAINT nonvalidated_generation '
        'CHECK (generation >= 0) NOT VALID'),
    'missing_default': (
        'ALTER TABLE real_global_authorization_fences '
        'ALTER COLUMN generation DROP DEFAULT'),
    'nullable': (
        'ALTER TABLE real_authorization_fences '
        'ALTER COLUMN updated_at DROP NOT NULL'),
    'extra_constraint': (
        'ALTER TABLE real_authorization_fences '
        'ADD CONSTRAINT restrictive_generation CHECK (generation < 100)'),
    'bad_global_singleton_constraint': (
        'ALTER TABLE real_global_authorization_fences '
        'DROP CONSTRAINT real_global_authorization_fences_id_check'),
}


@pytest.mark.parametrize('defect', sorted(_DEFECTS))
def test_f01_rejects_partial_tables_rolls_back_and_startup_fails_closed(
        isolated_f01, defect):
    dsn, schema, engine = isolated_f01
    db.Base.metadata.create_all(engine)  # All other application tables exist.
    with engine.begin() as conn:
        # Each entry consists only of fixed synthetic DDL in the disposable schema.
        for statement in _DEFECTS[defect].split('; '):
            conn.exec_driver_sql(statement)

    with pytest.raises(psycopg.Error, match='F01 incompatible'):
        _apply(dsn, schema)
    assert not _stamped(engine)  # Revision CREATE rolled back as well.
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT to_regclass('nexora_schema_revisions')")).scalar_one() is None

    # Even a falsely pre-stamped schema must fail closed at application startup.
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE nexora_schema_revisions ('
                          'revision VARCHAR(80) PRIMARY KEY, '
                          'applied_at TIMESTAMP WITH TIME ZONE '
                          'NOT NULL DEFAULT CURRENT_TIMESTAMP)'))
        conn.execute(text(
            "INSERT INTO nexora_schema_revisions(revision) "
            "VALUES ('f01_global_fencing_v1')"))
    with pytest.raises(RuntimeError, match='incompatível'):
        db.init_db()


def test_f01_bad_existing_revision_table_cannot_be_stamped(isolated_f01):
    dsn, schema, engine = isolated_f01
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE users (id INTEGER PRIMARY KEY)'))
        conn.execute(text('CREATE TABLE nexora_schema_revisions ('
                          'revision TEXT PRIMARY KEY, '
                          'applied_at TIMESTAMP WITH TIME ZONE NOT NULL '
                          'DEFAULT CURRENT_TIMESTAMP)'))
    with pytest.raises(psycopg.Error, match='F01 incompatible column'):
        _apply(dsn, schema)
    assert not _stamped(engine)
    with engine.connect() as conn:
        # Objects from earlier CREATE IF NOT EXISTS statements were rolled back.
        for table in ('real_authorization_fences', 'real_global_authorization_fences'):
            assert conn.execute(text('SELECT to_regclass(:name)'),
                                {'name': table}).scalar_one() is None


def test_f01_existing_valid_tables_with_invalid_singleton_roll_back(isolated_f01):
    dsn, schema, engine = isolated_f01
    db.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(
            'UPDATE real_global_authorization_fences SET generation=5 WHERE id=1'))
        conn.execute(text('DELETE FROM real_global_authorization_fences WHERE id=1'))
    # A missing singleton is safely seeded; no revision is stamped before that.
    _apply(dsn, schema)
    assert _stamped(engine)
    with engine.connect() as conn:
        assert conn.execute(text(
            'SELECT id, generation FROM real_global_authorization_fences')).all() == [(1, 0)]

"""Durable database. SQLite allowed only for LOCAL development; production requires Postgres."""
import os
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text, UniqueConstraint, CheckConstraint, Index, inspect, text, DDL, event
from sqlalchemy.orm import declarative_base, sessionmaker

DB_URL = os.getenv('DATABASE_URL', 'sqlite:///./nexora_local.db')
if DB_URL.startswith('postgres://'):
    DB_URL = 'postgresql+psycopg://' + DB_URL[len('postgres://'):]
if DB_URL.startswith('postgresql://'):
    DB_URL = 'postgresql+psycopg://' + DB_URL[len('postgresql://'):]
engine = create_engine(DB_URL, pool_pre_ping=True, connect_args={'check_same_thread':False} if DB_URL.startswith('sqlite:') else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

def utcnow(): return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    deriv_account_id = Column(String(100), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    stake = Column(String(20), default='0.50', nullable=False)
    martingale = Column(Boolean, default=False, nullable=False)
    multiplier = Column(String(20), default='2.00', nullable=False)
    stop_win = Column(String(20), default='0.00', nullable=False)
    stop_loss = Column(String(20), default='0.00', nullable=False)
    selected_mode = Column(String(16), default='demo', nullable=False)
    # Execution consent is stored separately; existing users do not auto-enroll.

class RobotAccess(Base):
    """One per-user server-side DEMO access entitlement.

    The grant clock is UTC and never depends on a browser or Deriv session.
    Renewals are explicit; merely logging in does not reset the expiry.
    """
    __tablename__='robot_access'
    user_id=Column(Integer,ForeignKey('users.id'),primary_key=True)
    access_kind=Column(String(24),nullable=False,default='trial_24h')
    starts_at=Column(DateTime(timezone=True),default=utcnow,nullable=False)
    expires_at=Column(DateTime(timezone=True),nullable=False)
    updated_at=Column(DateTime(timezone=True),default=utcnow,onupdate=utcnow,nullable=False)
    suspended=Column(Boolean,nullable=False,default=False)


class StripeAccessPurchase(Base):
    """One authenticated checkout; webhook fulfillment uses idempotent row lock."""
    __tablename__ = 'stripe_access_purchases'
    checkout_session_id = Column(String(255), primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    plan_id = Column(String(24), nullable=False)
    amount_cents = Column(Integer, nullable=False)
    status = Column(String(24), nullable=False, default='PENDING')
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    paid_at = Column(DateTime(timezone=True), nullable=True)


class ReferralLink(Base):
    """Opaque public referral code; never expose Deriv identifiers."""
    __tablename__='referral_links'
    user_id=Column(Integer,ForeignKey('users.id'),primary_key=True)
    code=Column(String(32),nullable=False,unique=True,index=True)
    created_at=Column(DateTime(timezone=True),default=utcnow,nullable=False)


class ReferralShortLink(Base):
    """Stable compact referral code. Existing referral links remain valid."""
    __tablename__='referral_short_links'
    user_id=Column(Integer,ForeignKey('users.id'),primary_key=True)
    slug=Column(String(12),nullable=False,unique=True,index=True)
    created_at=Column(DateTime(timezone=True),default=utcnow,nullable=False)


class ReferralCredit(Base):
    """At most one reward per invited account, committed atomically with expiry."""
    __tablename__='referral_credits'
    invitee_user_id=Column(Integer,ForeignKey('users.id'),primary_key=True)
    referrer_user_id=Column(Integer,ForeignKey('users.id'),nullable=False,index=True)
    credited_hours=Column(Integer,nullable=False,default=24)
    created_at=Column(DateTime(timezone=True),default=utcnow,nullable=False)


class ReferralAttribution(Base):
    """Durable first-visit OAuth attribution without exposing private tokens."""
    __tablename__='referral_attributions'
    invitee_user_id=Column(Integer,ForeignKey('users.id'),primary_key=True)
    referrer_user_id=Column(Integer,ForeignKey('users.id'),nullable=False,index=True)
    created_at=Column(DateTime(timezone=True),default=utcnow,nullable=False)


class AccessAdminEvent(Base):
    """Audit log for manual exception decisions and payment refunds."""
    __tablename__='access_admin_events'
    id=Column(Integer,primary_key=True)
    admin_user_id=Column(Integer,ForeignKey('users.id'),nullable=False,index=True)
    subject_user_id=Column(Integer,ForeignKey('users.id'),nullable=False,index=True)
    action=Column(String(32),nullable=False)
    delta_hours=Column(Integer,nullable=True)
    note=Column(String(255),nullable=False)
    created_at=Column(DateTime(timezone=True),default=utcnow,nullable=False)


class OwnerAdminEvent(Base):
    """Owner-only access interventions without requiring a Deriv identity."""
    __tablename__='owner_admin_events'
    id=Column(Integer,primary_key=True)
    subject_user_id=Column(Integer,ForeignKey('users.id'),nullable=False,index=True)
    action=Column(String(32),nullable=False)
    delta_hours=Column(Integer,nullable=True)
    note=Column(String(255),nullable=False)
    created_at=Column(DateTime(timezone=True),default=utcnow,nullable=False)


class RobotPlan(Base):
    """Saved onboarding preferences; experimental progressions are NOT execution rules."""
    __tablename__ = 'robot_plans'
    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    gale_count = Column(Integer, nullable=False, default=0)
    requested_martingale = Column(Boolean, nullable=False, default=False)
    requested_multiplier = Column(String(20), nullable=False, default='2.00')
    cycles_limit = Column(Integer, nullable=True)  # NULL means no user-selected cycle cap
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class RobotCycleParams(Base):
    """Separate extension avoids altering existing production Postgres tables."""
    __tablename__ = 'robot_cycle_params'
    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    multiplier = Column(String(20), nullable=False, default='2.00')



class AccountRiskSettings(Base):
    """Risk preferences per account. Saving preferences never authorizes an order.

    Legacy User/RobotPlan/RobotCycleParams are DEMO-only compatibility mirrors.
    REAL has no implicit migration or fallback to DEMO preferences.
    """
    __tablename__ = 'account_risk_settings'
    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    account_mode = Column(String(16), primary_key=True)
    account_id = Column(String(100), primary_key=True)
    stake = Column(String(20), nullable=False)
    stop_win = Column(String(20), nullable=False)
    stop_loss = Column(String(20), nullable=False)
    martingale = Column(Boolean, nullable=False, default=False)
    gale_count = Column(Integer, nullable=False, default=0)
    gale_multiplier = Column(String(20), nullable=False, default='2.00')
    cycles_limit = Column(Integer, nullable=False, default=0)
    cycle_multiplier = Column(String(20), nullable=False, default='2.00')
    revision = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)



class RobotRun(Base):
    """One opt-in run boundary; sequence reconstructed from official settled orders."""
    __tablename__ = 'robot_runs'
    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    after_order_id = Column(Integer, nullable=False, default=0)
    started_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class AutoPilotResumeConsent(Base):
    """Explicit DEMO-only opt-in for resuming after a process restart.

    New table rather than retroactively interpreting old RobotRun rows as consent.
    Cleared on stop, logout, account reconnection, or safety halt.
    """
    __tablename__ = 'autopilot_resume_consents'
    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    enabled_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class PilotValidationBatch(Base):
    """A separate, opt-in AutoPilot 2.0 experiment; old official ledger stays intact."""
    __tablename__ = 'pilot_validation_batches'
    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    after_order_id = Column(Integer, nullable=False, default=0)
    max_orders = Column(Integer, nullable=False, default=50)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class PilotStrategyChoice(Base):
    """Explicit owner-of-session choice; never enables execution by itself."""
    __tablename__ = 'pilot_strategy_choices'
    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    strategy = Column(String(40), nullable=False, default='standard')
    updated_at = Column(DateTime(timezone=True), default=utcnow,
                        onupdate=utcnow, nullable=False)


class AutomationProfile(Base):
    """Per-user requested automation profile. Settings alone never arm trading."""
    __tablename__='automation_profiles'
    user_id=Column(Integer,ForeignKey('users.id'),primary_key=True)
    market=Column(String(24),nullable=False,default='volatility')
    symbols_json=Column(Text,nullable=False,default='["1HZ100V"]')
    strategy=Column(String(40),nullable=False,default='pulso_v2')
    account_mode=Column(String(16),nullable=False,default='demo')
    expiry_minutes=Column(Integer,nullable=False,default=5)
    news_filter=Column(Boolean,nullable=False,default=False)
    news_minutes_before=Column(Integer,nullable=False,default=30)
    news_minutes_after=Column(Integer,nullable=False,default=30)
    min_payout=Column(String(12),nullable=False,default='88')
    max_daily_orders=Column(Integer,nullable=False,default=0)
    indicators_json=Column(Text,nullable=False,default='[]')
    direction=Column(String(10),nullable=False,default='AUTO')
    real_account_id=Column(String(100),nullable=True)
    preset_mode=Column(String(20),nullable=False,default='custom')
    updated_at=Column(DateTime(timezone=True),default=utcnow,onupdate=utcnow,nullable=False)


class RiskSession(Base):
    """Auditable manual DEMO risk reset boundary; historical orders stay untouched."""
    __tablename__ = 'risk_sessions'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    after_order_id = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (Index('ix_risk_sessions_user_id_id', 'user_id', 'id'),)



class LoginFlow(Base):
    __tablename__ = 'login_flows'
    state_hash = Column(String(64), primary_key=True)
    verifier_encrypted = Column(Text, nullable=False)
    referral_code = Column(String(32), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

class Session(Base):
    __tablename__ = 'sessions'
    session_hash = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    token_encrypted = Column(Text, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)

class PaperTrade(Base):
    __tablename__ = 'paper_trades'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    opened_at = Column(DateTime(timezone=True), default=utcnow)
    settled_at = Column(DateTime(timezone=True), default=utcnow)
    symbol = Column(String(50), nullable=False)
    direction = Column(String(10), nullable=False)
    stake = Column(String(20), nullable=False)
    entry = Column(String(40), nullable=False)
    exit = Column(String(40), nullable=False)
    pnl = Column(String(20), nullable=False)
    result = Column(String(10), nullable=False)
    __table_args__ = (Index('ix_paper_user_time','user_id','settled_at'),)


F01_REQUIRED_REVISION = 'f01_global_fencing_v1'



# The PostgreSQL startup gate must reject a stamped but structurally invalid
# F01 schema. These contracts also match the formal SQL migration; this path
# uses catalog SELECTs only and never alters the running database.
_F01_TABLE_COLUMNS = {
    'real_authorization_fences': {
        'user_id': ('integer', True, None),
        'generation': ('integer', True, '0'),
        'updated_at': ('timestamp with time zone', True, 'CURRENT_TIMESTAMP'),
    },
    'real_global_authorization_fences': {
        'id': ('integer', True, None),
        'generation': ('integer', True, '0'),
        'updated_at': ('timestamp with time zone', True, 'CURRENT_TIMESTAMP'),
    },
    'nexora_schema_revisions': {
        'revision': ('character varying(80)', True, None),
        'applied_at': ('timestamp with time zone', True, 'CURRENT_TIMESTAMP'),
    },
}
_F01_TABLE_PK = {
    'real_authorization_fences': ('user_id',),
    'real_global_authorization_fences': ('id',),
    'nexora_schema_revisions': ('revision',),
}
_F01_TABLE_CHECKS = {
    'real_authorization_fences': {'generation>=0'},
    'real_global_authorization_fences': {'generation>=0', 'id=1'},
    'nexora_schema_revisions': set(),
}


def _verify_f01_postgresql_contract(connection):
    """Strict catalog-only inspection for fencing/migration revision tables."""
    schema = connection.execute(text('SELECT current_schema()')).scalar_one()
    for table, expected_columns in _F01_TABLE_COLUMNS.items():
        rel = connection.execute(text("""
            SELECT c.oid, c.relkind
              FROM pg_catalog.pg_class c
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = :schema AND c.relname = :table
        """), {'schema': schema, 'table': table}).mappings().one_or_none()
        if rel is None or rel['relkind'] != 'r':
            raise RuntimeError('Esquema PostgreSQL incompatível F01: tabela ' + table)

        rows = connection.execute(text("""
            SELECT a.attname AS name,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS type,
                   a.attnotnull AS not_null,
                   pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS default_sql
              FROM pg_catalog.pg_attribute a
              LEFT JOIN pg_catalog.pg_attrdef d
                ON d.adrelid = a.attrelid AND d.adnum = a.attnum
             WHERE a.attrelid = :oid AND a.attnum > 0 AND NOT a.attisdropped
        """), {'oid': rel['oid']}).mappings().all()
        actual_columns = {
            row['name']: (row['type'], row['not_null'], row['default_sql'])
            for row in rows
        }
        if actual_columns != expected_columns:
            raise RuntimeError('Esquema PostgreSQL incompatível F01: colunas/tipos/defaults ' + table)

        constraints = connection.execute(text("""
            SELECT p.contype AS kind, p.convalidated AS validated,
                   p.condeferrable AS deferrable,
                   p.condeferred AS deferred, p.conislocal AS is_local,
                   p.connoinherit AS no_inherit,
                   (SELECT array_agg(a.attname ORDER BY u.ordinality)
                      FROM unnest(p.conkey) WITH ORDINALITY AS u(attnum, ordinality)
                      JOIN pg_catalog.pg_attribute a
                        ON a.attrelid = p.conrelid AND a.attnum = u.attnum) AS columns,
                   CASE WHEN p.contype = 'c' THEN
                     regexp_replace(lower(pg_catalog.pg_get_expr(p.conbin, p.conrelid)),
                                    '[[:space:]()]', '', 'g')
                   END AS check_expr,
                   rn.nspname AS ref_schema, rc.relname AS ref_table,
                   (SELECT array_agg(a.attname ORDER BY u.ordinality)
                      FROM unnest(p.confkey) WITH ORDINALITY AS u(attnum, ordinality)
                      JOIN pg_catalog.pg_attribute a
                        ON a.attrelid = p.confrelid AND a.attnum = u.attnum) AS ref_columns,
                   p.confupdtype AS on_update, p.confdeltype AS on_delete,
                   p.confmatchtype AS match_type,
                   i.indisprimary AS index_primary,
                   i.indisunique AS index_unique,
                   i.indisvalid AS index_valid,
                   i.indisready AS index_ready
              FROM pg_catalog.pg_constraint p
              LEFT JOIN pg_catalog.pg_class rc ON rc.oid = p.confrelid
              LEFT JOIN pg_catalog.pg_namespace rn ON rn.oid = rc.relnamespace
              LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = p.conindid
             WHERE p.conrelid = :oid
        """), {'oid': rel['oid']}).mappings().all()

        allowed_checks = _F01_TABLE_CHECKS[table]
        expected_constraint_count = 1 + len(allowed_checks) + (table == 'real_authorization_fences')
        if len(constraints) != expected_constraint_count:
            raise RuntimeError('Esquema PostgreSQL incompatível F01: constraints ' + table)
        checks_found = set()
        pk_found = fk_found = False
        for constraint in constraints:
            if (not constraint['validated'] or constraint['deferrable']
                    or constraint['deferred'] or not constraint['is_local']
                    or (constraint['kind'] == 'c' and constraint['no_inherit'])):
                raise RuntimeError('Esquema PostgreSQL incompatível F01: constraint não validada ' + table)
            columns = tuple(constraint['columns'] or ())
            if constraint['kind'] == 'p':
                if (pk_found or columns != _F01_TABLE_PK[table]
                        or not all(constraint[x] for x in
                                   ('index_primary', 'index_unique', 'index_valid', 'index_ready'))):
                    raise RuntimeError('Esquema PostgreSQL incompatível F01: PK ' + table)
                pk_found = True
            elif constraint['kind'] == 'c':
                expr = constraint['check_expr']
                if expr not in allowed_checks or expr in checks_found:
                    raise RuntimeError('Esquema PostgreSQL incompatível F01: CHECK ' + table)
                checks_found.add(expr)
            elif constraint['kind'] == 'f' and table == 'real_authorization_fences':
                if (fk_found or columns != ('user_id',)
                        or constraint['ref_schema'] != schema
                        or constraint['ref_table'] != 'users'
                        or tuple(constraint['ref_columns'] or ()) != ('id',)
                        or constraint['on_update'] != 'a'
                        or constraint['on_delete'] != 'a'
                        or constraint['match_type'] != 's'):
                    raise RuntimeError('Esquema PostgreSQL incompatível F01: FK ' + table)
                fk_found = True
            else:
                raise RuntimeError('Esquema PostgreSQL incompatível F01: constraint inesperada ' + table)
        if not pk_found or checks_found != allowed_checks or (
                table == 'real_authorization_fences' and not fk_found):
            raise RuntimeError('Esquema PostgreSQL incompatível F01: constraint ausente ' + table)


def verify_postgresql_schema(connection):
    """Read-only startup gate: PostgreSQL application processes NEVER run DDL.

    A migration administrator, separately authorized, must install the schema
    and stamp the required revision before the service may start.
    """
    inspector=inspect(connection)
    all_tables=set(inspector.get_table_names())
    required=set(Base.metadata.tables)
    required.add('nexora_schema_revisions')
    missing=sorted(required-all_tables)
    if missing:
        raise RuntimeError('Esquema PostgreSQL incompleto; migração formal obrigatória: '+', '.join(missing))
    for name, fields in {
        'real_authorization_fences':('user_id','generation','updated_at'),
        'real_global_authorization_fences':('id','generation','updated_at'),
        'real_robot_consents':('user_id','account_id','revoked_at'),
        'nexora_schema_revisions':('revision','applied_at'),
    }.items():
        found={c['name'] for c in inspector.get_columns(name)}
        if not set(fields).issubset(found):
            raise RuntimeError('Esquema PostgreSQL incompatível: '+name)
    _verify_f01_postgresql_contract(connection)
    revision=connection.execute(text(
        'SELECT revision FROM nexora_schema_revisions WHERE revision=:revision'
    ),{'revision':F01_REQUIRED_REVISION}).scalar_one_or_none()
    if revision!=F01_REQUIRED_REVISION:
        raise RuntimeError('Migração F01 não aplicada ou não autorizada')
    row=connection.execute(text(
        'SELECT generation FROM real_global_authorization_fences WHERE id=1'
    )).scalar_one_or_none()
    if row is None or row<0:
        raise RuntimeError('Geração global F01 ausente ou inválida; iniciar bloqueado')


def init_db():
    if engine.dialect.name == 'postgresql':
        # NO create_all, ALTER, migration stamping or other DDL in application
        # startup. Even a missing fencing table must fail closed.
        with engine.connect() as connection:
            verify_postgresql_schema(connection)
        return
    Base.metadata.create_all(engine)
    # Backwards-compatible additive migration. Never fabricate official times
    # for existing contracts or overwrite any pre-existing stored evidence.
    columns={col['name'] for col in inspect(engine).get_columns('demo_orders')}
    if 'broker_purchase_at' not in columns:
        with engine.begin() as conn:
            conn.execute(text('ALTER TABLE demo_orders ADD COLUMN broker_purchase_at TIMESTAMP WITH TIME ZONE'))
    flow_columns={col['name'] for col in inspect(engine).get_columns('login_flows')}
    if 'referral_code' not in flow_columns:
        with engine.begin() as conn:
            conn.execute(text('ALTER TABLE login_flows ADD COLUMN referral_code VARCHAR(32)'))
    profile_columns={col['name'] for col in inspect(engine).get_columns('automation_profiles')}
    if 'indicators_json' not in profile_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE automation_profiles ADD COLUMN indicators_json TEXT NOT NULL DEFAULT '[]'"))
    if 'direction' not in profile_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE automation_profiles ADD COLUMN direction VARCHAR(10) NOT NULL DEFAULT 'AUTO'"))
    if 'real_account_id' not in profile_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE automation_profiles ADD COLUMN real_account_id VARCHAR(100)"))
    if 'preset_mode' not in profile_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE automation_profiles ADD COLUMN preset_mode VARCHAR(20) NOT NULL DEFAULT 'custom'"))
    access_columns={col['name'] for col in inspect(engine).get_columns('robot_access')}
    if 'suspended' not in access_columns:
        with engine.begin() as conn:
            conn.execute(text('ALTER TABLE robot_access ADD COLUMN suspended BOOLEAN NOT NULL DEFAULT FALSE'))



class DemoOrder(Base):
    """Durable demo-only order ledger. An active slot is unique per user, even across workers.

    Never reuse an unknown slot without broker reconciliation. NULL active_slot allows
    multiple settled orders on Postgres and SQLite.
    """
    __tablename__ = 'demo_orders'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    request_key = Column(String(64), nullable=False)
    active_slot = Column(Integer, unique=True, nullable=True)
    state = Column(String(16), nullable=False, default='PENDING')
    contract_id = Column(String(100), nullable=True, unique=True)
    symbol = Column(String(50), nullable=False)
    direction = Column(String(10), nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    stake = Column(String(20), nullable=False)
    buy_price = Column(String(20), nullable=True)
    pnl = Column(String(30), nullable=True)
    broker_status = Column(String(40), nullable=True)
    broker_purchase_at = Column(DateTime(timezone=True), nullable=True)  # Deriv-reported, not request time
    error_summary = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint('user_id', 'request_key', name='uq_demo_order_request'),)


class RealOrder(Base):
    """Real-money order ledger. Never co-mingle DEMO results or pending slots.

    A unique per-account live slot survives restarts and unknown buy responses.
    Only a verified broker settlement releases the slot.
    """
    __tablename__ = 'real_orders'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    account_id = Column(String(100), nullable=False)
    request_key = Column(String(64), nullable=False)
    active_slot = Column(String(100), nullable=True, unique=True)
    state = Column(String(16), nullable=False, default='PENDING')
    contract_id = Column(String(100), nullable=True, unique=True)
    symbol = Column(String(50), nullable=False)
    direction = Column(String(10), nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    stake = Column(String(20), nullable=False)
    currency = Column(String(20), nullable=False)
    buy_price = Column(String(20), nullable=True)
    pnl = Column(String(30), nullable=True)
    broker_status = Column(String(40), nullable=True)
    broker_purchase_at = Column(DateTime(timezone=True), nullable=True)
    error_summary = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint('account_id', 'request_key', name='uq_real_order_request'),
        Index('ix_real_order_account_state', 'account_id', 'state'),
    )


class RealGlobalAuthorizationFence(Base):
    """Global epoch protecting cold-start revocation against older STARTs."""
    __tablename__ = 'real_global_authorization_fences'
    id = Column(Integer, primary_key=True)
    generation = Column(Integer, nullable=False, default=0, server_default=text('0'))
    updated_at = Column(DateTime(timezone=True), default=utcnow,
                        onupdate=utcnow, nullable=False,
                        server_default=text('CURRENT_TIMESTAMP'))
    __table_args__ = (CheckConstraint('id = 1'),
                      CheckConstraint('generation >= 0'))


# Explicit metadata.create_all is permitted ONLY in disposable/local test
# setup; production init_db() never creates this table or seeds this row.
event.listen(RealGlobalAuthorizationFence.__table__, 'after_create',
             DDL("INSERT INTO real_global_authorization_fences (id, generation, updated_at) "
                 "VALUES (1, 0, CURRENT_TIMESTAMP)"))


class RealAuthorizationFence(Base):
    """F01: durable, per-user generation invalidating pre-STOP START attempts.

    Never represents an authorization and cannot enable REAL by itself.
    """
    __tablename__ = 'real_authorization_fences'
    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    generation = Column(Integer, nullable=False, default=0, server_default=text('0'))
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow,
                        nullable=False, server_default=text('CURRENT_TIMESTAMP'))
    __table_args__ = (CheckConstraint('generation >= 0'),)


class RealRobotConsent(Base):
    """Opt-in REAL activation is separate from saved settings or DEMO consent."""
    __tablename__ = 'real_robot_consents'
    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    account_id = Column(String(100), nullable=False)
    max_stake = Column(String(20), nullable=False)
    stop_loss = Column(String(20), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)



class AutoPilotAttribution(Base):
    """Strategy attribution for verified broker orders (no simulated fills)."""
    __tablename__ = 'autopilot_attributions'
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('demo_orders.id'), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    strategy = Column(String(32), nullable=False)
    direction = Column(String(10), nullable=False)
    reason = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

class DemoOrderProgression(Base):
    """Snapshot of how an automatic DEMO order was generated, saved before broker I/O.

    A new separate table avoids altering existing Render Postgres tables.
    Historical trades without a snapshot are explicitly classified as unknown.
    """
    __tablename__ = 'demo_order_progressions'
    order_id = Column(Integer, ForeignKey('demo_orders.id'), primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    cycle_index = Column(Integer, nullable=False)
    gale_index = Column(Integer, nullable=False)
    gale_multiplier = Column(String(20), nullable=False)
    cycle_multiplier = Column(String(20), nullable=False)
    source = Column(String(20), nullable=False, default='autopilot')
    account_mode = Column(String(16), nullable=False, default='demo')
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

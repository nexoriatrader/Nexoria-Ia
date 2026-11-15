"""Durable database. SQLite allowed only for LOCAL development; production requires Postgres."""
import os
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text, UniqueConstraint, Index, inspect, text
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
    stake = Column(String(20), default='2.00', nullable=False)
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


def init_db():
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

"""Durable database. SQLite allowed only for LOCAL development; production requires Postgres."""
import os
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text, UniqueConstraint, Index, inspect, text, select
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
    operation_mode=Column(String(24),nullable=False,default='strategies')
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


class AutomationPanelProfile(Base):
    """Complete, independent user configuration for one executable panel."""
    __tablename__='automation_panel_profiles'
    user_id=Column(Integer,ForeignKey('users.id'),primary_key=True)
    panel_mode=Column(String(16),primary_key=True)
    operation_mode=Column(String(24),nullable=False,default='strategies')
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
    stake=Column(String(20),nullable=False,default='0.50')
    stop_win=Column(String(20),nullable=False,default='0.00')
    stop_loss=Column(String(20),nullable=False,default='0.00')
    requested_martingale=Column(Boolean,nullable=False,default=False)
    gale_count=Column(Integer,nullable=False,default=0)
    requested_multiplier=Column(String(20),nullable=False,default='2.00')
    cycles_limit=Column(Integer,nullable=False,default=0)
    cycle_multiplier=Column(String(20),nullable=False,default='2.00')
    updated_at=Column(DateTime(timezone=True),default=utcnow,onupdate=utcnow,nullable=False)


class AutomationExecutionState(Base):
    """Durable per-user exclusive panel reservation; never an auto-resume grant."""
    __tablename__='automation_execution_states'
    user_id=Column(Integer,ForeignKey('users.id'),primary_key=True)
    active_panel_mode=Column(String(16),nullable=True)
    account_mode=Column(String(16),nullable=True)
    reserved_at=Column(DateTime(timezone=True),nullable=True)
    updated_at=Column(DateTime(timezone=True),default=utcnow,onupdate=utcnow,nullable=False)


class RiskSession(Base):
    """Auditable manual DEMO risk reset boundary; historical orders stay untouched."""
    __tablename__ = 'risk_sessions'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    panel_mode = Column(String(16), nullable=True)
    after_order_id = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (Index('ix_risk_sessions_user_id_id', 'user_id', 'id'),)


class ManualSignalRun(Base):
    """Persistent per-user controller; session_number separates completed runs."""
    __tablename__='manual_signal_runs'
    user_id=Column(Integer,ForeignKey('users.id'),primary_key=True)
    session_number=Column(Integer,nullable=False,default=0)
    status=Column(String(16),nullable=False,default='IDLE')
    account_mode=Column(String(16),nullable=False,default='demo')
    real_account_id=Column(String(100),nullable=True)
    pause_reason=Column(String(255),nullable=True)
    started_at=Column(DateTime(timezone=True),nullable=True)
    paused_at=Column(DateTime(timezone=True),nullable=True)
    completed_at=Column(DateTime(timezone=True),nullable=True)
    updated_at=Column(DateTime(timezone=True),default=utcnow,onupdate=utcnow,nullable=False)


class ManualSignal(Base):
    """One durable signal line; broker orders remain in separate official ledgers."""
    __tablename__='manual_signals'
    id=Column(Integer,primary_key=True)
    user_id=Column(Integer,ForeignKey('users.id'),nullable=False,index=True)
    session_number=Column(Integer,nullable=False)
    capacity_slot=Column(Integer,nullable=True)
    line_number=Column(Integer,nullable=False)
    raw_line=Column(String(255),nullable=False)
    market=Column(String(32),nullable=False)
    timezone_name=Column(String(64),nullable=False)
    local_date=Column(String(10),nullable=False)
    local_time=Column(String(5),nullable=False)
    scheduled_at=Column(DateTime(timezone=True),nullable=False,index=True)
    symbol=Column(String(30),nullable=False)
    direction=Column(String(10),nullable=False)
    duration_minutes=Column(Integer,nullable=False)
    status=Column(String(16),nullable=False,default='SCHEDULED',index=True)
    reason=Column(String(255),nullable=True)
    request_key=Column(String(64),nullable=False,unique=True)
    ledger_mode=Column(String(16),nullable=True)
    ledger_order_id=Column(Integer,nullable=True)
    claim_token=Column(String(64),nullable=True)
    claimed_at=Column(DateTime(timezone=True),nullable=True)
    created_at=Column(DateTime(timezone=True),default=utcnow,nullable=False)
    updated_at=Column(DateTime(timezone=True),default=utcnow,onupdate=utcnow,nullable=False)
    __table_args__=(
        UniqueConstraint('user_id','session_number','capacity_slot',name='uq_manual_signal_capacity_slot'),
        Index('ix_manual_signal_queue_due','status','scheduled_at'),
        Index('ix_manual_signal_session','user_id','session_number','status'),
    )



class LoginFlow(Base):
    __tablename__ = 'login_flows'
    state_hash = Column(String(64), primary_key=True)
    verifier_encrypted = Column(Text, nullable=False)
    referral_code = Column(String(32), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

class ManualSignalBatch(Base):
    """Idempotent confirmation receipt; payload identity survives page reloads."""
    __tablename__='manual_signal_batches'
    id=Column(Integer,primary_key=True)
    user_id=Column(Integer,ForeignKey('users.id'),nullable=False,index=True)
    session_number=Column(Integer,nullable=False)
    idempotency_key=Column(String(64),nullable=False)
    payload_hash=Column(String(64),nullable=False)
    response_json=Column(Text,nullable=False)
    created_at=Column(DateTime(timezone=True),default=utcnow,nullable=False)
    __table_args__=(UniqueConstraint('user_id','session_number','idempotency_key',
        name='uq_manual_signal_batch_idempotency'),)


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


class CatalogerCandle(Base):
    """Public, user-independent Deriv candle cache, shared by all accounts."""
    __tablename__ = 'cataloger_candles'
    symbol = Column(String(30), primary_key=True)
    granularity_seconds = Column(Integer, primary_key=True)
    epoch = Column(Integer, primary_key=True)
    open_price = Column(String(40), nullable=False)
    high_price = Column(String(40), nullable=True)
    low_price = Column(String(40), nullable=True)
    close_price = Column(String(40), nullable=False)
    __table_args__ = (Index('ix_cataloger_candles_epoch', 'epoch'),)


class CatalogerHistoryState(Base):
    """Shared sync cursor and freshness marker for one symbol/timeframe."""
    __tablename__ = 'cataloger_history_states'
    symbol = Column(String(30), primary_key=True)
    granularity_seconds = Column(Integer, primary_key=True)
    synchronized_at = Column(DateTime(timezone=True), nullable=True)
    oldest_epoch = Column(Integer, nullable=True)
    newest_epoch = Column(Integer, nullable=True)


def _seed_panel_profile(user, legacy, plan, cycle, mode):
    """Copy legacy automation identity only to the panel it previously described."""
    current_mode=getattr(legacy,'operation_mode','strategies') if legacy else None
    copy_current=legacy is not None and current_mode==mode
    is_manual=mode=='manual'
    copy_strategy=copy_current and not is_manual
    copy_manual=copy_current and is_manual
    return AutomationPanelProfile(
        user_id=user.id,panel_mode=mode,
        operation_mode='manual' if is_manual else 'strategies',
        market=getattr(legacy,'market','volatility') if copy_strategy else 'volatility',
        symbols_json='[]' if is_manual else (
            getattr(legacy,'symbols_json','["1HZ100V"]') if copy_strategy else '["1HZ100V"]'),
        strategy='manual' if is_manual else (
            getattr(legacy,'strategy','pulso_v2') if copy_strategy else 'pulso_v2'),
        account_mode=getattr(legacy,'account_mode','demo') if copy_current else 'demo',
        expiry_minutes=0 if is_manual else (
            getattr(legacy,'expiry_minutes',5) if copy_strategy else 5),
        news_filter=getattr(legacy,'news_filter',False) if copy_strategy else False,
        news_minutes_before=getattr(legacy,'news_minutes_before',30) if copy_strategy else 30,
        news_minutes_after=getattr(legacy,'news_minutes_after',30) if copy_strategy else 30,
        min_payout=getattr(legacy,'min_payout','88') if copy_strategy else '88',
        direction=getattr(legacy,'direction','AUTO') if copy_strategy else 'AUTO',
        real_account_id=(getattr(legacy,'real_account_id',None)
                         if copy_current and getattr(legacy,'account_mode','demo')=='real' else None),
        preset_mode=getattr(legacy,'preset_mode','custom') if copy_strategy else 'custom',
        stake=user.stake,stop_win=user.stop_win,stop_loss=user.stop_loss,
        requested_martingale=bool(plan and plan.requested_martingale and copy_current),
        gale_count=plan.gale_count if plan and copy_current else 0,
        requested_multiplier=(plan.requested_multiplier if plan and copy_current else '2.00'),
        cycles_limit=(plan.cycles_limit or 0) if plan and copy_current else 0,
        cycle_multiplier=cycle.multiplier if cycle and copy_current else '2.00')


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
    if 'operation_mode' not in profile_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE automation_profiles ADD COLUMN operation_mode VARCHAR(24) NOT NULL DEFAULT 'strategies'"))
    access_columns={col['name'] for col in inspect(engine).get_columns('robot_access')}
    if 'suspended' not in access_columns:
        with engine.begin() as conn:
            conn.execute(text('ALTER TABLE robot_access ADD COLUMN suspended BOOLEAN NOT NULL DEFAULT FALSE'))
    consent_columns={col['name'] for col in inspect(engine).get_columns('real_robot_consents')}
    if 'panel_mode' not in consent_columns:
        with engine.begin() as conn:
            conn.execute(text('ALTER TABLE real_robot_consents ADD COLUMN panel_mode VARCHAR(16)'))
    for table in ('demo_orders','real_orders'):
        columns={col['name'] for col in inspect(engine).get_columns(table)}
        if 'source_panel_mode' not in columns:
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN source_panel_mode VARCHAR(16)'))
    risk_columns={col['name'] for col in inspect(engine).get_columns('risk_sessions')}
    if 'panel_mode' not in risk_columns:
        with engine.begin() as conn:
            conn.execute(text('ALTER TABLE risk_sessions ADD COLUMN panel_mode VARCHAR(16)'))
    # Profiles are additive and seeded only once. The old REAL consent table is
    # intentionally never read or copied here, and no run/consent is armed.
    with SessionLocal() as session:
        users=session.scalars(select(User)).all()
        for user in users:
            legacy=session.get(AutomationProfile,user.id)
            plan=session.get(RobotPlan,user.id)
            cycle=session.get(RobotCycleParams,user.id)
            for mode in ('manual','strategies'):
                if session.get(AutomationPanelProfile,(user.id,mode)) is not None:
                    continue
                session.add(_seed_panel_profile(user,legacy,plan,cycle,mode))
            if session.get(AutomationExecutionState,user.id) is None:
                session.add(AutomationExecutionState(user_id=user.id))
        session.commit()


def ensure_panel_profiles(session, user_id):
    """Create only absent panel rows from the still-preserved legacy settings."""
    user=session.get(User,user_id)
    if user is None:
        return None,None
    legacy=session.get(AutomationProfile,user_id)
    plan=session.get(RobotPlan,user_id)
    cycle=session.get(RobotCycleParams,user_id)
    rows=[]
    for mode in ('manual','strategies'):
        row=session.get(AutomationPanelProfile,(user_id,mode))
        if row is None:
            row=_seed_panel_profile(user,legacy,plan,cycle,mode)
            session.add(row)
        rows.append(row)
    if session.get(AutomationExecutionState,user_id) is None:
        session.add(AutomationExecutionState(user_id=user_id))
    session.flush()
    by_mode={row.panel_mode:row for row in rows}
    return by_mode.get('manual'),by_mode.get('strategies')



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
    source_panel_mode = Column(String(16), nullable=True)
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
    source_panel_mode = Column(String(16), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint('account_id', 'request_key', name='uq_real_order_request'),
        Index('ix_real_order_account_state', 'account_id', 'state'),
    )


class RealRobotConsent(Base):
    """Opt-in REAL activation is separate from saved settings or DEMO consent."""
    __tablename__ = 'real_robot_consents'
    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    account_id = Column(String(100), nullable=False)
    max_stake = Column(String(20), nullable=False)
    stop_loss = Column(String(20), nullable=False)
    panel_mode = Column(String(16), nullable=True)
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

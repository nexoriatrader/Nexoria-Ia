"""Durable database. SQLite allowed only for LOCAL development; production requires Postgres."""
import os
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text, UniqueConstraint, Index
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
    stop_win = Column(String(20), default='20.00', nullable=False)
    stop_loss = Column(String(20), default='10.00', nullable=False)
    selected_mode = Column(String(16), default='demo', nullable=False)
    # User switch is intentionally NOT persisted: all bots start OFF after restart.

class LoginFlow(Base):
    __tablename__ = 'login_flows'
    state_hash = Column(String(64), primary_key=True)
    verifier_encrypted = Column(Text, nullable=False)
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


def init_db(): Base.metadata.create_all(engine)


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

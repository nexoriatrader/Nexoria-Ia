"""Existing PR #54 I04 SQLAlchemy models, isolated from automatic app DDL.

This is the SAME DATABASE_URL/session and SAME I04 tables, not a parallel
knowledge store. Separate metadata prevents db.init_db() from silently
creating I04 tables in PostgreSQL; only formal schema migrations (or explicit
disposable-test setup) may create them. No user/account foreign keys.
"""
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, Text,
    UniqueConstraint, CheckConstraint, Index,
)
from sqlalchemy.orm import declarative_base

KnowledgeMetadata = declarative_base()

def utcnow():
    return datetime.now(timezone.utc)


class IntelligenceKnowledge(KnowledgeMetadata):
    __tablename__='intelligence_knowledge'
    id=Column(Integer,primary_key=True)
    identity_key=Column(String(64),nullable=False,unique=True)
    category=Column(String(24),nullable=False)
    market=Column(String(64),nullable=False)
    symbol=Column(String(30),nullable=False)
    timeframe_seconds=Column(Integer,nullable=False)
    subject=Column(String(100),nullable=False)
    model_version=Column(String(64),nullable=False)
    regime=Column(String(40),nullable=False)
    state=Column(String(24),nullable=False)
    revision=Column(Integer,nullable=False)
    payload_json=Column(Text,nullable=False)
    payload_digest=Column(String(64),nullable=False)
    valid_from=Column(DateTime(timezone=True),nullable=False)
    valid_until=Column(DateTime(timezone=True),nullable=False)
    next_review_at=Column(DateTime(timezone=True),nullable=False)
    reason_code=Column(String(100),nullable=False)
    created_at=Column(DateTime(timezone=True),nullable=False,default=utcnow)
    updated_at=Column(DateTime(timezone=True),nullable=False,default=utcnow,onupdate=utcnow)
    __table_args__=(CheckConstraint('revision >= 1',name='ck_knowledge_revision'),
                    CheckConstraint('timeframe_seconds >= 0',name='ck_knowledge_timeframe'),
                    CheckConstraint('valid_from < valid_until',name='ck_knowledge_validity'),
                    Index('ix_knowledge_search','category','market','symbol','state'))


class IntelligenceKnowledgeEvidence(KnowledgeMetadata):
    __tablename__='intelligence_knowledge_evidence'
    id=Column(Integer,primary_key=True)
    knowledge_id=Column(Integer,ForeignKey('intelligence_knowledge.id'),nullable=False,index=True)
    source_type=Column(String(32),nullable=False)
    source_ref=Column(String(160),nullable=False)
    source_digest=Column(String(64),nullable=False)
    evidence_summary_json=Column(Text,nullable=False)
    recorded_revision=Column(Integer,nullable=False)
    origin_group=Column(String(64),nullable=False)
    verified=Column(Boolean,nullable=False)
    observation_count=Column(Integer,nullable=False)
    group_size=Column(Integer,nullable=False)
    period_from=Column(DateTime(timezone=True),nullable=False)
    period_to=Column(DateTime(timezone=True),nullable=False)
    recorded_at=Column(DateTime(timezone=True),nullable=False,default=utcnow)
    __table_args__=(
        UniqueConstraint('knowledge_id','source_digest',name='uq_knowledge_evidence_digest'),
        CheckConstraint('recorded_revision >= 2',name='ck_knowledge_evidence_revision'),
        CheckConstraint('observation_count >= 1',name='ck_knowledge_evidence_observations'),
        CheckConstraint('group_size >= 0',name='ck_knowledge_evidence_group'),
        CheckConstraint('period_from <= period_to',name='ck_knowledge_evidence_period'),
    )


class IntelligenceKnowledgeDecision(KnowledgeMetadata):
    __tablename__='intelligence_knowledge_decisions'
    id=Column(Integer,primary_key=True)
    knowledge_id=Column(Integer,ForeignKey('intelligence_knowledge.id'),nullable=False,index=True)
    revision=Column(Integer,nullable=False)
    from_state=Column(String(24),nullable=True)
    to_state=Column(String(24),nullable=False)
    decision=Column(String(40),nullable=False)
    reason_code=Column(String(100),nullable=False)
    evidence_digest=Column(String(64),nullable=False)
    recorded_at=Column(DateTime(timezone=True),nullable=False,default=utcnow)
    __table_args__=(
        UniqueConstraint('knowledge_id','revision',name='uq_knowledge_decision_revision'),
        CheckConstraint('revision >= 1',name='ck_knowledge_decision_revision'),
    )

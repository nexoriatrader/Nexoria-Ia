"""I05 scheduled/event-triggered PUBLIC research via the existing research loop.

No parallel timer, broker session, financial execution, user dataset, new
cache or new table. Public training is bounded to one verified I04 MARKET
source and one reused I01 1000-candle request per cycle.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .. import db
from ..research_persistence import HistoricalResearchReport
from . import knowledge_base as kb
from .learning_engine import (
    VERSION, REASONS, LearningError, collect_public_history,
    learning_enabled, evaluate_public_regimes,
)
from .learning_registry import (
    stage_public_candidate, monitor_and_rollback, _reg_lock,
)

EVENT_PREFIX = "I05_EVT_"
CYCLE_KEY = "I05_PUBLIC_REGIME_CYCLE_V1"
MAX_PUBLIC_SOURCES = 1
MIN_CYCLE_INTERVAL = timedelta(hours=24)


def _event_key(market: str, symbol: str, timeframe_seconds: int):
    _,values=kb._identity(
        "MARKET",market,symbol,timeframe_seconds,
        "public_learning_event","i05_v1",
    )
    encoded=json.dumps(values,sort_keys=True,separators=(",",":"))
    return EVENT_PREFIX+hashlib.sha256(encoded.encode()).hexdigest()[:48]


def record_market_event(*, market: str, symbol: str, timeframe_seconds: int,
                        observed_regime: str,
                        now: datetime | None = None) -> dict:
    """Queue a public regime-change event durably; no learning in request path.

    This may be called by I04's explicit public-market invalidation hook.
    On reopening/absence of verified MARKET knowledge the cycle will skip.
    """
    if not learning_enabled():
        return {"state":"DISABLED","execution_allowed":False}
    now=kb._utc(now or db.utcnow())
    key=_event_key(market,symbol,timeframe_seconds)
    regime=kb._token(observed_regime,field="regime",max_len=40)
    data={
        "version":VERSION,"market":market,"symbol":symbol,
        "timeframe_seconds":timeframe_seconds,"observed_regime":regime,
        "reason_code":"PUBLIC_REGIME_CHANGE","created_at":now.isoformat(),
        "execution_allowed":False,
    }
    with db.SessionLocal() as session:
        _reg_lock(session,key)
        previous=session.scalar(select(HistoricalResearchReport).where(
            HistoricalResearchReport.research_key==key,
        ).order_by(HistoricalResearchReport.id.desc()).limit(1))
        if previous is not None and (
            kb._utc(previous.created_at)>now-timedelta(hours=1)
        ):
            return {"state":"DEDUPLICATED",
                    "event_id":previous.id,"execution_allowed":False}
        row=HistoricalResearchReport(
            research_key=key,created_at=now,
            period_from=now,period_to=now,
            payload_json=json.dumps(data,sort_keys=True,separators=(",",":")),
        )
        session.add(row)
        session.commit()
        return {"state":"QUEUED","event_id":row.id,"execution_allowed":False}


def _latest_audit(session):
    row=session.scalar(select(HistoricalResearchReport).where(
        HistoricalResearchReport.research_key==CYCLE_KEY,
    ).order_by(HistoricalResearchReport.id.desc()).limit(1))
    if row is None:
        return None,None
    try:
        payload=json.loads(row.payload_json)
        if (payload.get("version")!=VERSION
            or payload.get("execution_allowed") is not False):
            raise ValueError("invalid audit")
        return row,payload
    except (ValueError,TypeError) as exc:
        raise kb.KnowledgeUnavailable("Auditoria I05 corrompida") from exc


def _audit(*, event_id: int | None, market: str | None,
           symbol: str | None, timeframe_seconds: int | None,
           status: str, now: datetime, candidate_sha: str | None = None) -> dict:
    # No per-user details or financial metrics; public bounded metadata only.
    value={
        "version":VERSION,"event_id":event_id,
        "market":market,"symbol":symbol,
        "timeframe_seconds":timeframe_seconds,
        "state":status,"candidate_sha256":candidate_sha,
        "execution_allowed":False,
    }
    with db.SessionLocal() as session:
        row=HistoricalResearchReport(
            research_key=CYCLE_KEY,created_at=now,
            period_from=now,period_to=now,
            payload_json=json.dumps(value,sort_keys=True,separators=(",",":")),
        )
        session.add(row)
        session.commit()
        return {"audit_id":row.id,**value}


async def run_scheduled_cycle(*, now: datetime | None = None) -> dict:
    """One bounded periodic or event-triggered learning/monitoring pass.

    Explicit opt-in, I04 prerequisite, DB audit, no automatic promotion.
    An event has priority; otherwise the daily public cycle is eligible.
    """
    if not learning_enabled():
        return {"state":"DISABLED","execution_allowed":False}
    now=kb._utc(now or db.utcnow())
    with db.SessionLocal() as session:
        latest,payload=_latest_audit(session)
        last_event_id=(payload or {}).get("event_id") or 0
        event=session.scalar(select(HistoricalResearchReport).where(
            HistoricalResearchReport.research_key.like(EVENT_PREFIX+"%"),
            HistoricalResearchReport.id>last_event_id,
        ).order_by(HistoricalResearchReport.id.asc()).limit(1))
        if event is not None:
            request=json.loads(event.payload_json)
            if (request.get("version")!=VERSION
                or request.get("execution_allowed") is not False):
                raise kb.KnowledgeUnavailable("Evento público inválido")
            target=(request["market"],request["symbol"],
                    request["timeframe_seconds"])
            event_id=event.id
        else:
            if latest is not None and kb._utc(latest.created_at)>now-MIN_CYCLE_INTERVAL:
                return {"state":"NOT_DUE","execution_allowed":False}
            target=None
            event_id=None
        query=select(db.IntelligenceKnowledge).where(
            db.IntelligenceKnowledge.category=="MARKET",
            db.IntelligenceKnowledge.state=="ACTIVE",
        )
        if target is not None:
            query=query.where(
                db.IntelligenceKnowledge.market==target[0],
                db.IntelligenceKnowledge.symbol==target[1],
                db.IntelligenceKnowledge.timeframe_seconds==target[2],
            )
        sources=session.scalars(query.order_by(
            db.IntelligenceKnowledge.id.asc()).limit(MAX_PUBLIC_SOURCES)).all()
        selected=[
            (row.id,row.market,row.symbol,row.timeframe_seconds)
            for row in sources
        ]

    if not selected:
        return _audit(
            event_id=event_id,market=target[0] if target else None,
            symbol=target[1] if target else None,
            timeframe_seconds=target[2] if target else None,
            status="SKIPPED_NO_VALIDATED_MARKET_SOURCE",now=now,
        )
    knowledge_id,market,symbol,timeframe_seconds=selected[0]
    try:
        history=await collect_public_history(
            market=market,symbol=symbol,timeframe_seconds=timeframe_seconds,
            observed_at=int(now.timestamp()),
        )
        # This verifies current I04 source validity and observed public regime
        # again, after the I01 read, before any persistent model artifact.
        fresh=kb.get_knowledge(
            knowledge_id,now=now,
            observed_regime=history["observations"][-1]["regime"])
        if not fresh["reusable_for_research"]:
            raise LearningError("I04 current regime/expiry forbids training")
        monitored=monitor_and_rollback(
            market=market,symbol=symbol,timeframe_seconds=timeframe_seconds,
            history=history,now=now)
        candidate=stage_public_candidate(
            history,market_knowledge_id=knowledge_id,now=now)
        if candidate["state"]=="REJECTED_OOS":
            status="REJECTED_OOS_RESEARCH"
        else:
            status="CHALLENGER_STAGED_AWAITING_APPROVAL"
        return _audit(
            event_id=event_id,market=market,symbol=symbol,
            timeframe_seconds=timeframe_seconds,
            status=status,now=now,
            candidate_sha=candidate["report"]["model_id_sha256"],
        ) | {"monitoring_state":monitored["state"]}
    except (LearningError,kb.KnowledgeError):
        return _audit(
            event_id=event_id,market=market,symbol=symbol,
            timeframe_seconds=timeframe_seconds,
            status="SKIPPED_DATA_OR_GOVERNANCE",now=now,
        )

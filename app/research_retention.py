"""Bounded retention for disposable research snapshots.

Financial ledgers, execution history, user access and broker audits are never
touched. Each research key retains its newest 3 snapshots, regardless of age.
Older snapshots are deleted only when over 7 days old or above 10 per key.

IMPORTANT: Personal research-history keys share this table. Iterate metadata
in a single ordered query rather than issuing one SELECT per user/key; never
load potentially large report JSON while deciding what to retain.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from . import db
from .research_persistence import HistoricalResearchReport

KEEP_LATEST = 3
MAX_PER_KEY = 10
MAX_AGE_DAYS = 7
DELETE_BATCH_SIZE = 400


def cleanup_research_reports(*, now=None, session_factory=None):
    """Delete expired disposable research snapshots, with bounded SQL queries."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now.astimezone(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    factory = session_factory or db.SessionLocal
    with factory() as session:
        metadata = session.execute(
            select(
                HistoricalResearchReport.id,
                HistoricalResearchReport.research_key,
                HistoricalResearchReport.created_at,
            ).order_by(
                HistoricalResearchReport.research_key,
                HistoricalResearchReport.created_at.desc(),
                HistoricalResearchReport.id.desc(),
            )
        )
        previous_key = None
        ordinal = 0
        delete_ids = []
        for report_id, key, created_at in metadata:
            if key != previous_key:
                previous_key = key
                ordinal = 0
            timestamp = (created_at.replace(tzinfo=timezone.utc)
                         if created_at.tzinfo is None
                         else created_at.astimezone(timezone.utc))
            if ordinal >= KEEP_LATEST and (
                ordinal >= MAX_PER_KEY or timestamp < cutoff
            ):
                delete_ids.append(report_id)
            ordinal += 1

        for start in range(0, len(delete_ids), DELETE_BATCH_SIZE):
            session.execute(
                delete(HistoricalResearchReport).where(
                    HistoricalResearchReport.id.in_(
                        delete_ids[start:start + DELETE_BATCH_SIZE]
                    )
                )
            )
        session.commit()
        return len(delete_ids)

"""Bounded retention for disposable research snapshots.

Financial ledgers, execution history, user access and broker audits are never touched.
Each strategy/research key retains its newest 3 snapshots, regardless of age.
Older snapshots are deleted only when over 7 days old, or when above 10 per key.
"""
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, delete

from . import db
from .research_persistence import HistoricalResearchReport

KEEP_LATEST = 3
MAX_PER_KEY = 10
MAX_AGE_DAYS = 7


def cleanup_research_reports(*, now=None, session_factory=None):
    """Return the number of old research-only snapshots deleted."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    factory = session_factory or db.SessionLocal
    deleted = 0
    with factory() as session:
        keys = session.scalars(
            select(HistoricalResearchReport.research_key).distinct()
        ).all()
        for key in keys:
            # I05 uses the EXISTING research report table for immutable
            # artifact, release and event audit chains. Never prune those
            # records as ordinary disposable research snapshots.
            if key.startswith(("I05_ART_", "I05_REL_", "I05_EVT_")) or key == "I05_PUBLIC_REGIME_CYCLE_V1":
                continue
            rows = session.scalars(
                select(HistoricalResearchReport)
                .where(HistoricalResearchReport.research_key == key)
                .order_by(
                    HistoricalResearchReport.created_at.desc(),
                    HistoricalResearchReport.id.desc(),
                )
            ).all()
            for index, row in enumerate(rows):
                created = row.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if index >= KEEP_LATEST and (
                    index >= MAX_PER_KEY or created < cutoff
                ):
                    session.delete(row)
                    deleted += 1
        session.commit()
    return deleted

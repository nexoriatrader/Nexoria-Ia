"""P0-04: fail-closed restart normalization of persistent REAL consent.

A saved REAL account configuration or pre-crash consent is never authority to
resume a worker after an application restart. This module only changes the
local consent marker in the database: it does not import or contact a broker,
interpret financial results, release contract slots or enable trading.

Existing DEMO resume consent and all order records remain untouched.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import update

from . import db


def revoke_stale_real_consent_after_restart(session, *, now: datetime | None = None) -> int:
    """Revoke previously unrevoked REAL opt-in records on cold startup.

    A single conditional SQL UPDATE is safe to repeat after interrupted
    startup and safe if another service worker already revoked the same row.
    Caller provides a disposable/local session in tests. An unexpected DB
    failure propagates so startup cannot proceed with unverifiable consent.
    """
    instant = db.utcnow() if now is None else now
    try:
        result = session.execute(
            update(db.RealRobotConsent)
            .where(db.RealRobotConsent.revoked_at.is_(None))
            .values(revoked_at=instant)
            .execution_options(synchronize_session=False)
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    return result.rowcount

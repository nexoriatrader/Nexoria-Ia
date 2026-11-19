"""P0-04: fail-closed restart normalization of persistent REAL consent.

A saved REAL account configuration or pre-crash consent is never authority to
resume a worker after an application restart. This module only changes the
local consent marker in the database: it does not import or contact a broker,
interpret financial results, release contract slots or enable trading.

Existing DEMO resume consent and all order records remain untouched.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import select, update

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


@contextmanager
def locked_real_consent_for_send(user_id: int, account_id: str):
    """Coordinate a final consent check with cross-process revocation.

    PostgreSQL SELECT FOR UPDATE holds the consent row locked until this
    context exits. The caller MUST keep the context open across its existing
    broker submission, so that a committed revocation cannot interleave
    between the last check and an outbound request.

    This is defensive only: it cannot grant consent, enable feature flags,
    start workers, create orders, or change any order ledger entries.
    Production coordination requires PostgreSQL; fail closed on other engines
    (SQLite does not implement row-level SELECT FOR UPDATE).
    """
    with db.SessionLocal() as session:
        try:
            if session.get_bind().dialect.name != "postgresql":
                yield False
                return
            consent = session.scalar(
                select(db.RealRobotConsent)
                .where(db.RealRobotConsent.user_id == user_id)
                .with_for_update()
            )
            authorized = bool(
                consent
                and consent.account_id == account_id
                and consent.revoked_at is None
            )
            yield authorized
        finally:
            # Never commit from a purchase guard. Rollback/close releases the
            # row lock even on cancellation or a broker-side exception.
            session.rollback()

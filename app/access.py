"""DEMO-only robot access window. No payment credentials stored here.

Enable NEXORA_ACCESS_CONTROL_ENABLED=true only after product/onboarding
testing. One configurable trial (7 days by default, 24h for controlled testing) is issued on the first explicitly confirmed start.
All reads use server time; logout/login never renews the trial.
"""
import os
from datetime import datetime, timedelta, timezone
from . import db


def trial_duration_hours():
    """Seven-day commercial trial; a controlled 24h pilot can set 24."""
    try:
        hours=int(os.getenv('NEXORA_TRIAL_HOURS','168'))
    except ValueError:
        hours=168
    return hours if hours in (24,168) else 168


def access_control_enabled():
    return os.getenv('NEXORA_ACCESS_CONTROL_ENABLED','false').lower()=='true'


def utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def access_status(session, user_id, now=None):
    now=utc(now or db.utcnow())
    grant=session.get(db.RobotAccess,user_id)
    if not access_control_enabled():
        return {'enforced':False,'allowed':True,'status':'TEST_MODE',
                'access_kind':grant.access_kind if grant else None,
                'expires_at':utc(grant.expires_at).isoformat() if grant else None}
    if grant is None:
        return {'enforced':True,'allowed':False,'status':'TRIAL_AVAILABLE',
                'access_kind':None,'expires_at':None,'trial_hours':trial_duration_hours()}
    expiry=utc(grant.expires_at)
    allowed=now<expiry
    return {'enforced':True,'allowed':allowed,
            'status':'ACTIVE' if allowed else 'EXPIRED',
            'access_kind':grant.access_kind,'expires_at':expiry.isoformat()}


def start_trial_on_first_robot_start(session,user_id,now=None):
    """Create exactly once, transaction must be committed by caller.

    Never extend an existing grant even when expired. Paid/manual extension
    is a separate, explicitly authorized administration operation.
    """
    if not access_control_enabled():
        return access_status(session,user_id,now)
    now=utc(now or db.utcnow())
    grant=session.get(db.RobotAccess,user_id)
    if grant is None:
        grant=db.RobotAccess(user_id=user_id,access_kind='trial_'+str(trial_duration_hours())+'h',
                             starts_at=now,expires_at=now+timedelta(hours=trial_duration_hours()))
        session.add(grant)
        session.flush()
    return access_status(session,user_id,now)

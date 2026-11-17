"""Shared DEMO/REAL robot access window. No payment credentials stored here.

Enable NEXORA_ACCESS_CONTROL_ENABLED=true only after product/onboarding
testing. Trial is 24 hours by default, starting with first verified Deriv registration for new users.
All reads use server time; logout/login never renews the trial.
"""
import os
import secrets
from sqlalchemy import select
from datetime import datetime, timedelta, timezone
from . import db


def trial_duration_hours():
    """24-hour launch trial; older 7-day grants are preserved as stored."""
    try:
        hours=int(os.getenv('NEXORA_TRIAL_HOURS','24'))
    except (ValueError, TypeError):
        hours=24
    return hours if hours in (24,168) else 24


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
        suspended=bool(grant and grant.suspended)
        return {'enforced':False,'allowed':not suspended,
                'status':'SUSPENDED' if suspended else 'TEST_MODE',
                'access_kind':grant.access_kind if grant else None,
                'expires_at':utc(grant.expires_at).isoformat() if grant else None}
    if grant is None:
        return {'enforced':True,'allowed':True,'status':'TRIAL_AVAILABLE',
                'access_kind':None,'expires_at':None,'trial_hours':trial_duration_hours()}
    expiry=utc(grant.expires_at)
    allowed=now<expiry and not grant.suspended
    return {'enforced':True,'allowed':allowed,
            'status':'SUSPENDED' if grant.suspended else ('ACTIVE' if allowed else 'EXPIRED'),
            'access_kind':grant.access_kind,'suspended':bool(grant.suspended),'expires_at':expiry.isoformat(),
            'server_time':now.isoformat(),'remaining_seconds':max(0,int((expiry-now).total_seconds())) if allowed else 0}


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


def ensure_referral_link(session, user_id):
    """Stable, opaque ID for sharing. The internal DB id is never in the link."""
    row=session.get(db.ReferralLink,user_id)
    if row is None:
        row=db.ReferralLink(user_id=user_id,code=secrets.token_urlsafe(12))
        session.add(row)
        session.flush()
    return row.code


def ensure_short_referral_slug(session,user_id):
    """Stable ~8-character alias; server lookup maintains legacy codes."""
    row=session.get(db.ReferralShortLink,user_id)
    if row is None:
        row=db.ReferralShortLink(user_id=user_id,slug=secrets.token_urlsafe(6))
        session.add(row)
        session.flush()
    return row.slug


def referral_report(session, user_id):
    code=ensure_referral_link(session,user_id)
    slug=ensure_short_referral_slug(session,user_id)
    credits=session.scalars(select(db.ReferralCredit).where(
        db.ReferralCredit.referrer_user_id==user_id)).all()
    return {'user_code':code,'short_code':slug,'confirmed_referrals':len(credits),
            'bonus_hours':sum(c.credited_hours for c in credits)}


def register_first_login(session, user, *, created_new, referred_code=None, now=None):
    """Called only after valid Deriv OAuth, in same DB transaction as login.

    New identity only, no repeated login credits, no self-referral.
    New verified accounts receive the configured trial even while enforcement is in pilot mode.
    A referral reward is an atomic, once-per-invitee ledger entry.
    """
    now=utc(now or db.utcnow())
    ensure_referral_link(session,user.id)
    if not created_new:
        return
    if session.get(db.RobotAccess,user.id) is None:
        hours=trial_duration_hours()
        session.add(db.RobotAccess(user_id=user.id,access_kind='trial_'+str(hours)+'h',
                 starts_at=now,expires_at=now+timedelta(hours=hours)))
        session.flush()
    if not referred_code:
        return
    link=session.scalar(select(db.ReferralLink).where(
        db.ReferralLink.code==referred_code))
    if link is None or link.user_id==user.id:
        return
    # First completed OAuth registration is the only point of attribution.
    session.add(db.ReferralAttribution(invitee_user_id=user.id,
                        referrer_user_id=link.user_id,created_at=now))
    if session.get(db.ReferralCredit,user.id) is not None:
        return
    access=session.get(db.RobotAccess,link.user_id)
    if access is None:
        # Existing testers can earn a day from first qualifying referral.
        access=db.RobotAccess(user_id=link.user_id,access_kind='referral',
                              starts_at=now,expires_at=now)
        session.add(access)
        session.flush()
    if access.suspended:
        return  # A suspended referrer cannot gain access via referrals.
    baseline=max(now,utc(access.expires_at))
    access.expires_at=baseline+timedelta(hours=24)
    session.add(db.ReferralCredit(invitee_user_id=user.id,
                   referrer_user_id=link.user_id,credited_hours=24,created_at=now))
    session.flush()

"""Admin authentication and one-time automatic referral qualification."""
from datetime import datetime,timedelta,timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import access,db,admin_auth


def make_db():
    engine=create_engine('sqlite:///:memory:')
    db.Base.metadata.create_all(engine)
    return engine,sessionmaker(bind=engine)


def test_referral_24h_once_per_first_oauth(monkeypatch):
    monkeypatch.delenv('NEXORA_ACCESS_CONTROL_ENABLED',raising=False)
    monkeypatch.setenv('NEXORA_TRIAL_HOURS','24')
    engine,factory=make_db()
    t=datetime(2026,11,14,14,0,tzinfo=timezone.utc)
    with factory() as session:
        owner=db.User(deriv_account_id='V-FIRST')
        session.add(owner);session.flush()
        access.register_first_login(session,owner,created_new=True,now=t)
        code=access.ensure_referral_link(session,owner.id)
        owner_until=access.utc(session.get(db.RobotAccess,owner.id).expires_at)
        assert owner_until==t+timedelta(hours=24)
        invited=db.User(deriv_account_id='V-INVITED')
        session.add(invited);session.flush()
        access.register_first_login(session,invited,created_new=True,
                                    referred_code=code,now=t+timedelta(minutes=7))
        session.commit()
        assert access.referral_report(session,owner.id)['confirmed_referrals']==1
        assert access.utc(session.get(db.RobotAccess,owner.id).expires_at)==owner_until+timedelta(hours=24)
        assert session.get(db.ReferralCredit,invited.id).credited_hours==24
        access.register_first_login(session,invited,created_new=False,
                                    referred_code=code,now=t+timedelta(hours=3))
        session.commit()
        assert access.referral_report(session,owner.id)['confirmed_referrals']==1
        assert access.utc(session.get(db.RobotAccess,owner.id).expires_at)==owner_until+timedelta(hours=24)
    engine.dispose()


def test_self_referral_and_unknown_code_never_credit(monkeypatch):
    monkeypatch.delenv('NEXORA_ACCESS_CONTROL_ENABLED',raising=False)
    engine,factory=make_db()
    with factory() as session:
        u=db.User(deriv_account_id='V-SELF');session.add(u);session.flush()
        code=access.ensure_referral_link(session,u.id)
        access.register_first_login(session,u,created_new=True,referred_code=code)
        stranger=db.User(deriv_account_id='V-NO-CODE');session.add(stranger);session.flush()
        access.register_first_login(session,stranger,created_new=True,referred_code='thisisnotarealcode')
        session.commit()
        assert access.referral_report(session,u.id)['confirmed_referrals']==0
    engine.dispose()


def test_suspended_referrer_gets_no_unlock(monkeypatch):
    monkeypatch.delenv('NEXORA_ACCESS_CONTROL_ENABLED',raising=False)
    engine,factory=make_db()
    with factory() as session:
        u=db.User(deriv_account_id='V-SUSPENDED');session.add(u);session.flush()
        access.register_first_login(session,u,created_new=True)
        code=access.ensure_referral_link(session,u.id)
        grant=session.get(db.RobotAccess,u.id)
        grant.suspended=True
        invite=db.User(deriv_account_id='V-NO-REWARD');session.add(invite);session.flush()
        access.register_first_login(session,invite,created_new=True,referred_code=code)
        session.commit()
        assert access.referral_report(session,u.id)['confirmed_referrals']==0
        assert access.access_status(session,u.id)['status']=='SUSPENDED'
        assert access.access_status(session,u.id)['allowed'] is False
    engine.dispose()


def test_owner_login_uses_environment_hash_and_signed_ttl(monkeypatch):
    import hashlib
    salt=b'unit-test-salt!!'
    rounds=350000
    expected=hashlib.pbkdf2_hmac('sha256',b'test-secret',salt,rounds)
    monkeypatch.setenv('NEXORA_ADMIN_PASSWORD_HASH',
        f'pbkdf2_sha256${rounds}${salt.hex()}${expected.hex()}')
    monkeypatch.setenv('NEXORA_ADMIN_SESSION_SECRET','test-key-for-unit-testing-only')
    assert admin_auth.verify_password('test-secret')
    assert not admin_auth.verify_password('wrong')
    token=admin_auth.session_token(now=100000)
    assert admin_auth.valid_session(token,now=100001)
    assert not admin_auth.valid_session(token,now=100000+8*3600+1)
    assert not admin_auth.valid_session('invalid')

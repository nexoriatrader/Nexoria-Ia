"""Server-side access rules: no payments or production trading are enabled."""
from datetime import datetime,timedelta,timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import access,db


def test_trial_24h_expires_without_resetting_at_login(monkeypatch):
    monkeypatch.setenv('NEXORA_ACCESS_CONTROL_ENABLED','true')
    monkeypatch.setenv('NEXORA_TRIAL_HOURS','24')
    engine=create_engine('sqlite:///:memory:')
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine)
    t=datetime(2026,11,14,12,0,tzinfo=timezone.utc)
    with factory() as sess:
        user=db.User(deriv_account_id='V-ACCESS-01')
        sess.add(user)
        sess.commit()
        assert access.access_status(sess,user.id,now=t)['status']=='TRIAL_AVAILABLE'
        assert access.access_status(sess,user.id,now=t)['allowed'] is True
        first=access.start_trial_on_first_robot_start(sess,user.id,now=t)
        sess.commit()
        assert first['status']=='ACTIVE'
        assert first['remaining_seconds']==86400
        assert first['access_kind']=='trial_24h'
        assert first['expires_at']==(t+timedelta(hours=24)).isoformat()
        second=access.start_trial_on_first_robot_start(
            sess,user.id,now=t+timedelta(hours=12))
        assert second['expires_at']==first['expires_at']
        assert access.access_status(sess,user.id,now=t+timedelta(hours=24))['status']=='EXPIRED'
        assert access.start_trial_on_first_robot_start(
            sess,user.id,now=t+timedelta(days=5))['allowed'] is False
    engine.dispose()


def test_default_is_seven_days_and_gate_is_off(monkeypatch):
    monkeypatch.delenv('NEXORA_ACCESS_CONTROL_ENABLED',raising=False)
    monkeypatch.delenv('NEXORA_TRIAL_HOURS',raising=False)
    assert access.trial_duration_hours()==168
    engine=create_engine('sqlite:///:memory:')
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine)
    with factory() as sess:
        status=access.start_trial_on_first_robot_start(sess,99001)
        assert status['status']=='TEST_MODE' and status['allowed']
        assert sess.get(db.RobotAccess,99001) is None
    engine.dispose()


def test_expiry_check_zero_seconds_and_referral_extension(monkeypatch):
    monkeypatch.setenv('NEXORA_TRIAL_HOURS','24')  # Explicit legacy-trial scenario.
    monkeypatch.setenv('NEXORA_ACCESS_CONTROL_ENABLED','true')
    engine=create_engine('sqlite:///:memory:')
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine)
    t=datetime(2026,11,14,12,0,tzinfo=timezone.utc)
    with factory() as sess:
        user=db.User(deriv_account_id='V-EXPIRY')
        sess.add(user)
        sess.flush()
        access.register_first_login(sess,user,created_new=True,now=t)
        sess.commit()
        assert access.access_status(sess,user.id,now=t+timedelta(hours=23))['remaining_seconds']==3600
        assert not access.access_status(sess,user.id,now=t+timedelta(hours=24))['allowed']
        assert access.access_status(sess,user.id,now=t+timedelta(hours=24))['remaining_seconds']==0
        # No second 24h trial on subsequent sign-ins or robot starts.
        access.start_trial_on_first_robot_start(sess,user.id,now=t+timedelta(days=3))
        assert access.access_status(sess,user.id,now=t+timedelta(days=3))['status']=='EXPIRED'
    engine.dispose()

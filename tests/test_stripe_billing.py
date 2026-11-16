"""Offline Stripe billing safety tests; no API keys or live charges."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db
from app.stripe_billing import PLANS, apply_paid_checkout, stripe_signature_valid


def fixture(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'billing.db'))
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    with factory() as s:
        u=db.User(deriv_account_id='BILLING_TEST_DEMO',selected_mode='demo')
        s.add(u)
        s.flush()
        s.add(db.RobotAccess(user_id=u.id,access_kind='trial_24h',
                             starts_at=datetime(2026,11,16,tzinfo=timezone.utc),
                             expires_at=datetime(2026,11,21,tzinfo=timezone.utc)))
        s.add(db.StripeAccessPurchase(checkout_session_id='cs_test_local',
             user_id=u.id,plan_id='mensal',amount_cents=4990,status='PENDING'))
        s.commit()
        return engine,factory,u.id


def paid_object(uid, **overrides):
    values={'id':'cs_test_local','mode':'payment','currency':'brl',
            'amount_total':4990,'payment_status':'paid',
            'client_reference_id':str(uid),
            'metadata':{'user_id':str(uid),'plan_id':'mensal'}}
    values.update(overrides)
    return values


def test_valid_stripe_signature_and_rejects_tampering_or_stale_timestamp():
    payload=b'{"type":"checkout.session.completed"}'
    timestamp=1794859200
    secret='whsec_unit_test_secret'
    digest=hmac.new(secret.encode(),str(timestamp).encode()+b'.'+payload,hashlib.sha256).hexdigest()
    header=f't={timestamp},v1=wrong,v1={digest}'
    assert stripe_signature_valid(payload,header,secret,now=timestamp)
    assert not stripe_signature_valid(payload+b'x',header,secret,now=timestamp)
    assert not stripe_signature_valid(payload,header,secret,now=timestamp+301)
    assert not stripe_signature_valid(payload,'',secret,now=timestamp)
    assert not stripe_signature_valid(payload,'t=bad,v1=123',secret,now=timestamp)


def test_checkout_fulfillment_exactly_once_extends_existing_time(tmp_path):
    engine,factory,uid=fixture(tmp_path)
    now=datetime(2026,11,16,16,0,tzinfo=timezone.utc)
    try:
        with factory() as s:
            assert apply_paid_checkout(s,paid_object(uid),now=now)=='applied'
            s.commit()
        with factory() as s:
            grant=s.get(db.RobotAccess,uid)
            assert grant.expires_at.replace(tzinfo=timezone.utc)==datetime(2026,12,21,tzinfo=timezone.utc)
            assert grant.access_kind=='paid_mensal'
            assert apply_paid_checkout(s,paid_object(uid),now=now+timedelta(hours=1))=='already_applied'
            s.commit()
        with factory() as s:
            assert s.get(db.RobotAccess,uid).expires_at.replace(tzinfo=timezone.utc)==datetime(2026,12,21,tzinfo=timezone.utc)
    finally:
        engine.dispose()


def test_checkout_rejects_other_user_amount_currency_plan_and_unpaid(tmp_path):
    engine,factory,uid=fixture(tmp_path)
    try:
        cases=[{'payment_status':'unpaid'},{'currency':'usd'},
               {'amount_total':2990},{'client_reference_id':str(uid+1)},
               {'metadata':{'user_id':str(uid),'plan_id':'quinzenal'}},
               {'metadata':{'user_id':str(uid+1),'plan_id':'mensal'}}]
        with factory() as s:
            for changed in cases:
                assert apply_paid_checkout(s,paid_object(uid,**changed))=='invalid_payment'
            assert apply_paid_checkout(s,paid_object(uid,id='cs_unknown'))=='unknown_checkout'
            assert s.get(db.StripeAccessPurchase,'cs_test_local').status=='PENDING'
            s.commit()
    finally:
        engine.dispose()


def test_payment_does_not_unban_suspended_access(tmp_path):
    engine,factory,uid=fixture(tmp_path)
    try:
        with factory() as s:
            grant=s.get(db.RobotAccess,uid)
            grant.suspended=True
            s.commit()
        with factory() as s:
            assert apply_paid_checkout(s,paid_object(uid))=='applied'
            s.commit()
        with factory() as s:
            assert s.get(db.RobotAccess,uid).suspended is True
    finally:
        engine.dispose()


def test_checkout_catalog_fixed_to_configured_brl_amounts():
    assert PLANS['quinzenal']=={'name':'NEXORA Quinzenal — 15 dias','days':15,'cents':2990}
    assert PLANS['mensal']['days']==30 and PLANS['mensal']['cents']==4990
    assert PLANS['trimestral']['days']==90 and PLANS['trimestral']['cents']==9900

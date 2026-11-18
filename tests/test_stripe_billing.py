"""Offline Stripe billing safety tests; no API keys or live charges."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db
from app.stripe_billing import PLANS, apply_paid_checkout, stripe_signature_valid, billing_enabled


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
    values={'id':'cs_test_local','mode':'payment','currency':'brl', 'livemode':False,
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
        cases=[{'payment_status':'unpaid'},{'currency':'usd'}, {'livemode':True},
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


def test_temporary_fifty_cent_plan_adds_exactly_one_hour_and_is_idempotent(tmp_path):
    assert PLANS['teste_1h']=={'name':'NEXORA — Teste Stripe 1 hora',
                               'hours':1,'cents':50}
    engine,factory,uid=fixture(tmp_path)
    now=datetime(2026,11,18,12,0,tzinfo=timezone.utc)
    try:
        with factory() as sess:
            order=sess.get(db.StripeAccessPurchase,'cs_test_local')
            order.plan_id='teste_1h'
            order.amount_cents=50
            sess.commit()
        payment=paid_object(uid,amount_total=50,
                    metadata={'user_id':str(uid),'plan_id':'teste_1h'})
        with factory() as sess:
            assert apply_paid_checkout(sess,payment,now=now)=='applied'
            sess.commit()
            expiry=sess.get(db.RobotAccess,uid).expires_at.replace(tzinfo=timezone.utc)
            assert expiry==datetime(2026,11,21,1,0,tzinfo=timezone.utc)
            assert apply_paid_checkout(sess,payment,now=now)=='already_applied'
            assert sess.get(db.RobotAccess,uid).expires_at.replace(tzinfo=timezone.utc)==expiry
        with factory() as sess:
            assert sess.get(db.StripeAccessPurchase,'cs_test_local').status=='PAID'
    finally:
        engine.dispose()


def test_temporary_plan_rejects_unpaid_or_incorrect_amount(tmp_path):
    engine,factory,uid=fixture(tmp_path)
    try:
        with factory() as sess:
            order=sess.get(db.StripeAccessPurchase,'cs_test_local')
            order.plan_id='teste_1h'
            order.amount_cents=50
            sess.commit()
        valid=paid_object(uid,amount_total=50,
                    metadata={'user_id':str(uid),'plan_id':'teste_1h'})
        with factory() as sess:
            assert apply_paid_checkout(sess,dict(valid,payment_status='unpaid'))=='invalid_payment'
            assert apply_paid_checkout(sess,dict(valid,amount_total=49))=='invalid_payment'
            assert sess.get(db.StripeAccessPurchase,'cs_test_local').status=='PENDING'
    finally:
        engine.dispose()


def test_stripe_test_mode_never_activates_public_paid_access(monkeypatch):
    monkeypatch.setenv('NEXORA_STRIPE_BILLING_ENABLED','true')
    monkeypatch.setenv('STRIPE_WEBHOOK_SECRET','whsec_test_fixture')
    monkeypatch.setenv('STRIPE_SECRET_KEY','sk_test_fixture')
    monkeypatch.setenv('NEXORA_STRIPE_MODE','test')
    monkeypatch.setenv('NEXORA_PUBLIC_URL','https://nexoria-ia.onrender.com')
    assert not billing_enabled()
    monkeypatch.setenv('NEXORA_PUBLIC_URL','https://preview.example.com')
    assert not billing_enabled()
    monkeypatch.setenv('NEXORA_PUBLIC_URL','http://localhost:8000')
    assert billing_enabled()


def test_stripe_live_mode_requires_explicit_live_keys(monkeypatch):
    monkeypatch.setenv('NEXORA_STRIPE_BILLING_ENABLED','true')
    monkeypatch.setenv('STRIPE_WEBHOOK_SECRET','whsec_live_fixture')
    monkeypatch.setenv('NEXORA_PUBLIC_URL','https://nexoria-ia.onrender.com')
    monkeypatch.setenv('STRIPE_SECRET_KEY','sk_test_fixture')
    monkeypatch.setenv('NEXORA_STRIPE_MODE','live')
    assert not billing_enabled()
    monkeypatch.setenv('STRIPE_SECRET_KEY','sk_live_fixture')
    assert billing_enabled()
    monkeypatch.delenv('NEXORA_STRIPE_MODE')
    assert not billing_enabled()


def test_paid_checkout_rejects_mismatched_livemode(tmp_path, monkeypatch):
    monkeypatch.setenv('NEXORA_STRIPE_MODE','live')
    engine,factory,uid=fixture(tmp_path)
    try:
        with factory() as session:
            assert apply_paid_checkout(session,paid_object(uid))=='invalid_payment'
            assert session.get(db.StripeAccessPurchase,'cs_test_local').status=='PENDING'
            session.commit()
    finally:
        engine.dispose()


def test_stripe_checkout_rejects_mismatched_api_mode(monkeypatch):
    import asyncio
    import httpx
    from app import stripe_billing

    monkeypatch.setenv('NEXORA_STRIPE_MODE','live')

    class FakeStripeClient:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return False
        async def post(self,url,**kwargs):
            return httpx.Response(200,request=httpx.Request('POST',url),
                json={'id':'cs_test_wrong_mode','livemode':False,
                      'url':'https://checkout.stripe.com/c/test'})

    monkeypatch.setattr(stripe_billing.httpx,'AsyncClient',
                        lambda **kwargs:FakeStripeClient())
    import pytest
    with pytest.raises(RuntimeError,match='Modo do Checkout incompatível'):
        asyncio.run(stripe_billing.create_checkout(
            'sk_live_fixture',user_id=1,plan_id='mensal',
            site_url='https://nexoria-ia.onrender.com'))


def test_webhook_rejects_signed_test_event_while_live_mode_is_required(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    import time

    monkeypatch.setenv('NEXORA_STRIPE_BILLING_ENABLED','true')
    monkeypatch.setenv('NEXORA_STRIPE_MODE','live')
    monkeypatch.setenv('STRIPE_SECRET_KEY','sk_live_fixture')
    monkeypatch.setenv('STRIPE_WEBHOOK_SECRET','whsec_local_fixture')
    monkeypatch.setenv('NEXORA_PUBLIC_URL','https://nexoria-ia.onrender.com')
    body=json.dumps({'type':'checkout.session.completed',
        'livemode':False,'data':{'object':{'id':'cs_test_mock','livemode':False}}}).encode()
    now=int(time.time())
    digest=hmac.new(b'whsec_local_fixture',
        str(now).encode()+b'.'+body,hashlib.sha256).hexdigest()
    header=f't={now},v1={digest}'
    result=TestClient(app).post('/api/billing/stripe/webhook',content=body,
        headers={'stripe-signature':header,'content-type':'application/json'})
    assert result.status_code==400
    assert result.json()['detail']=='Modo de pagamento incompatível com o ambiente'

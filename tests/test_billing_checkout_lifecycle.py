"""Offline purchase lifecycle: signed Stripe events, customer isolation and UX.

No external checkout is created, no real payment is sent.
"""
import hashlib
import hmac
import json
import time
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db, main
from app.security import encrypt, hashsecret


def billing_client(tmp_path,monkeypatch):
    engine=create_engine('sqlite:///'+str(tmp_path/'stripe_lifecycle.sqlite'))
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,'SessionLocal',factory)
    monkeypatch.setenv('NEXORA_STRIPE_BILLING_ENABLED','true')
    monkeypatch.setenv('NEXORA_STRIPE_MODE','test')
    monkeypatch.setenv('NEXORA_PUBLIC_URL','http://localhost:8000')
    monkeypatch.setenv('STRIPE_SECRET_KEY','sk_test_fixture_not_live')
    monkeypatch.setenv('STRIPE_WEBHOOK_SECRET','whsec_fixture')
    with factory() as sess:
        u=db.User(deriv_account_id='VSTRIPE1')
        other=db.User(deriv_account_id='VSTRIPE2')
        sess.add_all((u,other));sess.flush()
        uid=u.id
        sess.add(db.Session(session_hash=hashsecret('stripe-valid-session'),user_id=uid,
            token_encrypted=encrypt('mock-token'),
            expires_at=db.utcnow()+timedelta(hours=2)))
        sess.add(db.StripeAccessPurchase(checkout_session_id='cs_test_purchase_12345',
                user_id=uid,plan_id='mensal',amount_cents=4990,status='PENDING'))
        sess.add(db.StripeAccessPurchase(checkout_session_id='cs_test_purchase_other',
                user_id=other.id,plan_id='quinzenal',amount_cents=2990,status='PENDING'))
        sess.commit()
    c=TestClient(main.app)
    c.cookies.set('nexora_sid','stripe-valid-session')
    return c,factory,uid,engine


def sign_event(payload):
    body=json.dumps(payload,separators=(',',':')).encode('utf-8')
    timestamp=int(time.time())
    digest=hmac.new(b'whsec_fixture',
                    str(timestamp).encode()+b'.'+body,hashlib.sha256).hexdigest()
    return body,{'stripe-signature':f't={timestamp},v1={digest}',
                 'content-type':'application/json'}


def stripe_event(kind, uid, *, paid=False):
    session={'id':'cs_test_purchase_12345','livemode':False,
             'mode':'payment','currency':'brl','amount_total':4990,
             'payment_status':'paid' if paid else 'unpaid',
             'client_reference_id':str(uid),
             'metadata':{'user_id':str(uid),'plan_id':'mensal'}}
    return {'id':'evt_mock_'+kind.replace('.','_'),'type':kind,
            'livemode':False,'data':{'object':session}}


def test_checkout_status_requires_login_and_same_user(tmp_path,monkeypatch):
    client,db_session,uid,engine=billing_client(tmp_path,monkeypatch)
    try:
        ok=client.get('/api/billing/checkout/status',
                      params={'session_id':'cs_test_purchase_12345'})
        assert ok.status_code==200
        assert ok.json()['status']=='PENDING'
        assert ok.json()['confirmed'] is False
        assert ok.json()['access_expires_at'] is None
        assert client.get('/api/billing/checkout/status',
            params={'session_id':'cs_test_purchase_other'}).status_code==404
        assert client.get('/api/billing/checkout/status',
            params={'session_id':'garbage'}).status_code==404
        client.cookies.clear()
        assert client.get('/api/billing/checkout/status',
            params={'session_id':'cs_test_purchase_12345'}).status_code==401
    finally:
        client.close()
        engine.dispose()


def test_webhook_grants_paid_days_only_once_then_status_confirms(tmp_path,monkeypatch):
    client,factory,uid,engine=billing_client(tmp_path,monkeypatch)
    try:
        # Browser redirects alone must not mark paid.
        assert client.get('/api/billing/checkout/status',
            params={'session_id':'cs_test_purchase_12345'}).json()['status']=='PENDING'
        event=stripe_event('checkout.session.completed',uid,paid=True)
        body,headers=sign_event(event)
        paid=client.post('/api/billing/stripe/webhook',content=body,headers=headers)
        assert paid.status_code==200
        assert paid.json()['status']=='applied'
        record=client.get('/api/billing/checkout/status',
            params={'session_id':'cs_test_purchase_12345'}).json()
        assert record['confirmed'] is True
        assert record['status']=='PAID'
        assert record['access_expires_at']
        with factory() as sess:
            expiry=sess.get(db.RobotAccess,uid).expires_at
        duplicate=client.post('/api/billing/stripe/webhook',content=body,headers=headers)
        assert duplicate.status_code==200
        assert duplicate.json()['status']=='already_applied'
        with factory() as sess:
            assert sess.get(db.RobotAccess,uid).expires_at==expiry
    finally:
        client.close()
        engine.dispose()


def test_failed_or_expired_checkout_never_extends_access(tmp_path,monkeypatch):
    client,factory,uid,engine=billing_client(tmp_path,monkeypatch)
    try:
        for kind,expected in (
            ('checkout.session.async_payment_failed','FAILED'),
            ('checkout.session.expired','EXPIRED')):
            with factory() as sess:
                sess.get(db.StripeAccessPurchase,'cs_test_purchase_12345').status='PENDING'
                sess.commit()
            raw,headers=sign_event(stripe_event(kind,uid))
            result=client.post('/api/billing/stripe/webhook',content=raw,headers=headers)
            assert result.status_code==200
            assert result.json()['status']==expected
            assert client.get('/api/billing/checkout/status',
                params={'session_id':'cs_test_purchase_12345'}).json()['status']==expected
            with factory() as sess:
                assert sess.get(db.RobotAccess,uid) is None
        # Never downgrade a settled purchase due to delayed/out-of-order event.
        with factory() as sess:
            sess.get(db.StripeAccessPurchase,'cs_test_purchase_12345').status='PAID'
            sess.commit()
        raw,headers=sign_event(stripe_event('checkout.session.expired',uid))
        res=client.post('/api/billing/stripe/webhook',content=raw,headers=headers)
        assert res.status_code==200
        with factory() as sess:
            assert sess.get(db.StripeAccessPurchase,
                            'cs_test_purchase_12345').status=='PAID'
    finally:
        client.close()
        engine.dispose()


def test_webhook_rejects_unauthentic_failure_notification(tmp_path,monkeypatch):
    client,factory,uid,engine=billing_client(tmp_path,monkeypatch)
    try:
        body,headers=sign_event(stripe_event('checkout.session.expired',uid))
        headers['stripe-signature']='t=1,v1=forged'
        assert client.post('/api/billing/stripe/webhook',
            content=body,headers=headers).status_code==400
        with factory() as sess:
            assert sess.get(db.StripeAccessPurchase,
                            'cs_test_purchase_12345').status=='PENDING'
    finally:
        client.close()
        engine.dispose()


def test_payment_return_ui_waits_for_verified_webhook():
    html=(Path(__file__).resolve().parents[1]/'app'/'index.html').read_text()
    assert 'function clearStripeCheckoutQuery()' in html
    assert '/api/billing/checkout/status?session_id=' in html
    assert "if(result.status==='PAID')" in html
    assert "else if(paymentResult==='success'||paymentResult==='cancelled')" in html
    assert 'Aguardando confirmação oficial da Stripe' in html

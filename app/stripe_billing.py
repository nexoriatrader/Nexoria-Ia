"""Stripe-hosted one-time access purchases (no automatic recurring billing).

All prices/durations are server-owned. Access is credited ONLY by a verified
Stripe webhook with payment_status=paid and exact expected amount/currency.
"""
import hashlib
import hmac
import json
import os
import time
from datetime import timedelta

import httpx

from . import db
from .access import utc

# Do not accept prices or validity from clients or Stripe session metadata.
PLANS = {
    'quinzenal': {'name': 'NEXORA Quinzenal — 15 dias', 'days': 15, 'cents': 2990},
    'mensal': {'name': 'NEXORA Mensal — 30 dias', 'days': 30, 'cents': 4990},
    'trimestral': {'name': 'NEXORA Trimestral — 90 dias', 'days': 90, 'cents': 9900},
    # Remove after one successful end-to-end real checkout verification.
    'teste_1h': {'name': 'NEXORA — Teste Stripe 1 hora', 'hours': 1, 'cents': 50},
}


def stripe_expected_livemode():
    """Select test or live mode explicitly; default to test for offline fixtures."""
    return os.getenv('NEXORA_STRIPE_MODE', 'test').strip().lower() == 'live'


def billing_enabled():
    """Fail closed unless keys, mode and public environment agree."""
    if os.getenv('NEXORA_STRIPE_BILLING_ENABLED', '').lower() != 'true':
        return False
    mode=os.getenv('NEXORA_STRIPE_MODE', '').strip().lower()
    if mode not in ('test','live'):
        return False
    secret=os.getenv('STRIPE_SECRET_KEY', '')
    webhook=os.getenv('STRIPE_WEBHOOK_SECRET', '')
    if not webhook.startswith('whsec_'):
        return False
    if mode=='live' and not secret.startswith(('sk_live_','rk_live_')):
        return False
    if mode=='test' and not secret.startswith(('sk_test_','rk_test_')):
        return False
    # A test Checkout must never grant paid access on the public NEXORA site.
    public_url=os.getenv('NEXORA_PUBLIC_URL', 'https://nexoria-ia.onrender.com').rstrip('/')
    if mode=='test' and public_url.startswith('https://'):
        return False
    return True


def stripe_signature_valid(payload: bytes, header: str, secret: str, *, now=None):
    """Stripe-Signature: signed timestamp + raw body with 5-minute tolerance."""
    if not secret or not header:
        return False
    parts = {}
    for part in header.split(','):
        if '=' in part:
            key, value = part.split('=', 1)
            parts.setdefault(key.strip(), []).append(value.strip())
    try:
        timestamp = int(parts['t'][0])
    except (KeyError, IndexError, ValueError, TypeError):
        return False
    current = int(time.time() if now is None else now)
    if abs(current - timestamp) > 300:
        return False
    digest = hmac.new(secret.encode(), str(timestamp).encode() + b'.' + payload,
                      hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(digest, candidate) for candidate in parts.get('v1', []))


async def create_checkout(secret_key: str, *, user_id: int, plan_id: str, site_url: str):
    plan = PLANS[plan_id]
    data = {
        'mode': 'payment',
        'client_reference_id': str(user_id),
        'success_url': site_url + '/?payment=success&session_id={CHECKOUT_SESSION_ID}',
        'cancel_url': site_url + '/?payment=cancelled',
        'line_items[0][quantity]': '1',
        'line_items[0][price_data][currency]': 'brl',
        'line_items[0][price_data][unit_amount]': str(plan['cents']),
        'line_items[0][price_data][product_data][name]': plan['name'],
        'metadata[user_id]': str(user_id),
        'metadata[plan_id]': plan_id,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                'https://api.stripe.com/v1/checkout/sessions',
                data=data, auth=(secret_key, ''),
                headers={'Idempotency-Key': 'nexora-' + str(user_id) + '-' + os.urandom(12).hex()},
            )
            response.raise_for_status()
            obj = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError('Não foi possível iniciar o Checkout da Stripe') from exc
    if not isinstance(obj.get('id'), str) or not isinstance(obj.get('url'), str):
        raise RuntimeError('Resposta incompleta da Stripe')
    if obj.get('livemode') is not stripe_expected_livemode():
        raise RuntimeError('Modo do Checkout incompatível com o ambiente')
    if not obj['url'].startswith('https://checkout.stripe.com/'):
        raise RuntimeError('Destino de pagamento inesperado')
    return obj


def apply_paid_checkout(session, stripe_checkout, *, now=None):
    """Transactional, replay-safe fulfillment for one Stripe Checkout Session.

    Expects caller to commit transaction. Re-delivered webhook is idempotent.
    """
    sid = stripe_checkout.get('id')
    if not isinstance(sid, str):
        return 'ignored'
    order = session.get(db.StripeAccessPurchase, sid, with_for_update=True)
    if order is None:
        return 'unknown_checkout'
    plan = PLANS.get(order.plan_id)
    if not plan:
        return 'unknown_plan'
    if (stripe_checkout.get('livemode') is not stripe_expected_livemode()
        or stripe_checkout.get('payment_status') != 'paid'
        or stripe_checkout.get('mode') != 'payment'
        or stripe_checkout.get('currency') != 'brl'
        or stripe_checkout.get('amount_total') != plan['cents']
        or order.amount_cents != plan['cents']
        or str(stripe_checkout.get('client_reference_id')) != str(order.user_id)
        or str((stripe_checkout.get('metadata') or {}).get('user_id')) != str(order.user_id)
        or (stripe_checkout.get('metadata') or {}).get('plan_id') != order.plan_id):
        return 'invalid_payment'
    if order.status == 'PAID':
        return 'already_applied'
    now = utc(now or db.utcnow())
    grant = session.get(db.RobotAccess, order.user_id, with_for_update=True)
    if grant is None:
        grant = db.RobotAccess(user_id=order.user_id, access_kind='paid_' + order.plan_id,
                               starts_at=now, expires_at=now)
        session.add(grant)
        session.flush()
    # Suspension stays suspended; buying time never overrides administrative blocks.
    grant.expires_at = max(utc(grant.expires_at), now) + (
        timedelta(hours=plan['hours']) if 'hours' in plan
        else timedelta(days=plan['days']))
    grant.access_kind = 'paid_' + order.plan_id
    order.status = 'PAID'
    order.paid_at = now
    session.flush()
    return 'applied'

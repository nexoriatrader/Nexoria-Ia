"""Opt-in REAL automated scanner: separate order state, bounded concurrent scans.

No REAL robot is automatically resumed after server restart. All purchases
require a validated account, user risk consent and durable REAL reservation.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from decimal import Decimal
from datetime import timezone

from sqlalchemy import select

from . import db
from .access import access_status
from .automation_signals import confirmed_automation_signal
from .real_broker import (RealExecutionError, RealPurchaseUncertain,
                          buy_real_contract, real_account_balance,
                          real_contract_status)
from .real_ledger import (
    ACTIVE, RealOrderGuardError, cancel_unsubmitted_real_order,
    confirm_real_order, mark_real_unknown, public_real_order,
    reserve_real_order, settle_real_order,
)
from .security import decrypt

logger=logging.getLogger('nexora.real')
running: set[int]=set()
reasons: dict[int,str]={}
last_scan: dict[int,float]={}
_user_locks: dict[int,asyncio.Lock]={}
_reconcile_lock=asyncio.Lock()
_MAX_REAL_CONCURRENT_SCANS=3


def real_available():
    """REAL requires two independent, explicit server-side release gates.

    First gate is a technical feature toggle. The second is a deliberate
    production release approval, defaulting to OFF so a code deploy never
    activates real-money orders merely because an old toggle persisted.
    Individual authenticated account consent is still required separately.
    """
    return (
        os.getenv('NEXORA_REAL_AUTOPILOT_ENABLED', 'false').strip().lower() == 'true'
        and os.getenv('NEXORA_REAL_RELEASE_APPROVED', 'false').strip().lower() == 'true'
    )


def user_lock(uid):
    return _user_locks.setdefault(uid,asyncio.Lock())


def latest_token(sess, uid):
    now=db.utcnow()
    for auth in sess.scalars(select(db.Session).where(
        db.Session.user_id==uid).order_by(db.Session.created_at.desc())):
        expiry=auth.expires_at
        if expiry.tzinfo is None:
            expiry=expiry.replace(tzinfo=timezone.utc)
        if expiry<=now:
            continue
        try:
            return decrypt(auth.token_encrypted)
        except Exception:
            continue
    return None


def disarm_real(uid,why):
    running.discard(uid)
    reasons[uid]=why
    with db.SessionLocal() as sess:
        consent=sess.get(db.RealRobotConsent,uid)
        if consent and consent.revoked_at is None:
            consent.revoked_at=db.utcnow()
            sess.commit()


def _candidate_window(candidate):
    if not isinstance(candidate,dict):
        return None
    if candidate.get('direction') not in ('CALL','PUT'):
        return None
    if not candidate.get('symbol'):
        return None
    bar=candidate.get('last_bar_epoch')
    if not isinstance(bar,(int,float)):
        return None
    expiry=bar+60+12
    if time.time()>expiry:
        return None
    return expiry


async def _user_scan(uid):
    async with user_lock(uid):
        if uid not in running or not real_available():
            return
        # Prevent ten REAL robots from constantly re-running identical M1
        # analysis when the latest candle has not changed.
        loop_time=asyncio.get_running_loop().time()
        if loop_time-last_scan.get(uid,-1e12)<4:
            return
        last_scan[uid]=loop_time
        with db.SessionLocal() as sess:
            user=sess.get(db.User,uid)
            profile=sess.get(db.AutomationProfile,uid)
            consent=sess.get(db.RealRobotConsent,uid)
            if (not user or not profile or not consent
                or consent.revoked_at is not None
                or profile.account_mode!='real'
                or profile.real_account_id!=consent.account_id):
                disarm_real(uid,'Conta ou autorização REAL inválida')
                return
            if not access_status(sess,uid)['allowed']:
                disarm_real(uid,'Acesso ao robô expirado')
                return
            if profile.news_filter:
                reasons[uid]='Filtro de notícias indisponível; sem compra REAL'
                return
            if profile.strategy not in ('pulso_v2','trend','rsi'):
                disarm_real(uid,'Estratégia REAL inválida')
                return
            orders=sess.scalars(select(db.RealOrder).where(
                db.RealOrder.account_id==consent.account_id)).all()
            if any(x.state in ACTIVE for x in orders):
                reasons[uid]='Contrato REAL aberto ou incerto; aguardando confirmação'
                return
            token=latest_token(sess,uid)
            if not token:
                disarm_real(uid,'Sessão Deriv expirada; reconecte a conta REAL')
                return
            # Snapshot immutable values before broker network requests.
            account_id=consent.account_id
            stake=user.stake
            min_ratio=str(Decimal('1')+Decimal(profile.min_payout)/Decimal('100'))
            stop_loss=consent.stop_loss
            selected_symbols=list(json.loads(profile.symbols_json))
            if not selected_symbols or len(selected_symbols)>5:
                disarm_real(uid,'Seleção de ativos REAL inválida')
                return
            # Daily Stop Win/Loss is enforced inside the locked, durable
            # reservation transaction, not against a lifetime P/L snapshot.
        try:
            candidate=await confirmed_automation_signal(profile)
        except Exception as exc:
            logger.warning('REAL market scan unavailable: %s',type(exc).__name__)
            reasons[uid]='Dados de mercado indisponíveis; sem entrada REAL'
            return
        deadline=_candidate_window(candidate)
        if deadline is None:
            reasons[uid]='Aguardando sinal confirmado'
            return
        if candidate['symbol'] not in selected_symbols:
            reasons[uid]='Sinal fora dos ativos autorizados'
            return
        # Real account was verified as USD and funded at manual activation.
        # Reconnecting for a balance on every M1 signal would miss the entry
        # deadline. The authenticated purchase proposal and Deriv enforce funds.
        expiries=candidate.get('expiry_options') or [candidate.get('minutes')]
        expiries=[int(x) for x in expiries if x in (1,3,5,15)]
        if not expiries:
            reasons[uid]='Nenhuma expiração válida'
            return
        # Conservative rollout: use a single Deriv-confirmed duration. Actual
        # payout is rechecked on the streamed purchase proposal before buy.
        expiry=expiries[0]
        if uid not in running or time.time()>deadline:
            reasons[uid]='Sinal expirado antes da entrada REAL'
            return
        with db.SessionLocal() as sess:
            user=sess.get(db.User,uid)
            try:
                order,created=reserve_real_order(
                    sess,user,account_id=account_id,
                    request_key='real-'+secrets.token_hex(16),
                    symbol=candidate['symbol'],direction=candidate['direction'],
                    duration=expiry,stake=stake,currency='USD')
            except RealOrderGuardError as exc:
                reasons[uid]=str(exc)
                if 'Stop Loss' in str(exc) or 'Stop Win' in str(exc):
                    disarm_real(uid,str(exc))
                return
            order_id=order.id
        if uid not in running or time.time()>deadline:
            with db.SessionLocal() as sess:
                order=sess.get(db.RealOrder,order_id)
                cancel_unsubmitted_real_order(sess,order,'Desligado ou sinal expirado antes do envio')
            return
        reasons[uid]='Solicitando contrato REAL à Deriv'
        try:
            receipt=await buy_real_contract(
                token,account_id,symbol=candidate['symbol'],
                contract_type=candidate['direction'],stake=stake,
                duration=expiry,currency='USD',
                min_payout_ratio=min_ratio,
                latest_buy_send_epoch=deadline)
            with db.SessionLocal() as sess:
                order=sess.get(db.RealOrder,order_id)
                confirm_real_order(sess,order,receipt)
            reasons[uid]='Contrato REAL confirmado; aguardando liquidação'
        except RealPurchaseUncertain:
            with db.SessionLocal() as sess:
                mark_real_unknown(sess,sess.get(db.RealOrder,order_id))
            disarm_real(uid,'Compra REAL sem confirmação; novas entradas bloqueadas até reconciliação')
        except RealExecutionError as exc:
            # This exception is restricted to PRE-BUY validation failures by
            # real_broker. After the outbound buy, all failures are uncertain.
            with db.SessionLocal() as sess:
                cancel_unsubmitted_real_order(sess,sess.get(db.RealOrder,order_id),str(exc))
            reasons[uid]='Proposta REAL rejeitada; nenhuma compra enviada'
        except Exception as exc:
            # The broker might have received the buy. Never assume no trade.
            with db.SessionLocal() as sess:
                mark_real_unknown(sess,sess.get(db.RealOrder,order_id))
            disarm_real(uid,'Erro REAL incerto; confira contratos na Deriv')
            logger.error('REAL execution uncertain: %s',type(exc).__name__)


async def real_tick():
    if not real_available() or not running:
        return
    sem=asyncio.Semaphore(_MAX_REAL_CONCURRENT_SCANS)
    async def one(uid):
        async with sem:
            try:
                await _user_scan(uid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception('REAL scanner unexpected halt for user=%s',uid)
                disarm_real(uid,'Falha de segurança REAL; robô desligado')
    await asyncio.gather(*(one(uid) for uid in tuple(running)))


async def real_loop():
    while True:
        try:
            await real_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('REAL scheduler error')
        await asyncio.sleep(1)


async def reconcile_real_once():
    with db.SessionLocal() as sess:
        due=sess.scalars(select(db.RealOrder).where(
            db.RealOrder.state.in_(('CONFIRMED','UNKNOWN')),
            db.RealOrder.contract_id.is_not(None)).order_by(db.RealOrder.id.desc()).limit(100)).all()
        def is_due(order):
            if order.state=='UNKNOWN':
                return True
            when=order.broker_purchase_at
            if when is None:
                # Some valid buy receipts omit purchase_time. The locally
                # recorded reservation time is only a polling trigger, NEVER
                # evidence of settlement. Broker is_sold/profit remain required.
                when=order.created_at
            if when is None:
                return False
            # SQLite integration tests load UTC timestamps without tzinfo;
            # Postgres provides timezone-aware timestamps. Handle both.
            if when.tzinfo is None:
                when=when.replace(tzinfo=timezone.utc)
            return (db.utcnow()-when).total_seconds()>=order.duration_minutes*60-3
        due_ids=[o.id for o in due if is_due(o)]
    sem=asyncio.Semaphore(3)
    async def one(order_id):
        async with sem:
            with db.SessionLocal() as sess:
                order=sess.get(db.RealOrder,order_id)
                if not order or order.state not in ACTIVE or not order.contract_id:
                    return
                token=latest_token(sess,order.user_id)
                if not token:
                    return
                account_id,contract_id=order.account_id,order.contract_id
            try:
                result=await real_contract_status(token,account_id,contract_id)
            except RealExecutionError:
                return
            async with _reconcile_lock:
                with db.SessionLocal() as sess:
                    order=sess.get(db.RealOrder,order_id)
                    if order and order.state in ACTIVE:
                        try:
                            settle_real_order(sess,order,result['contract'])
                        except RealOrderGuardError:
                            return
    await asyncio.gather(*(one(x) for x in due_ids))


async def reconcile_real_loop():
    while True:
        try:
            await reconcile_real_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('REAL reconciliation error')
        await asyncio.sleep(5)

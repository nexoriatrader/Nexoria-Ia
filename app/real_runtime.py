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

from sqlalchemy import select, update

from . import db
from .access import access_status
from .automation_signals import confirmed_automation_signal
from .real_broker import (RealExecutionError, RealPurchaseUncertain,
                          buy_real_contract, real_account_balance,
                          real_contract_status)
from .real_ledger import (
    ACTIVE, RealOrderGuardError, cancel_unsubmitted_real_order,
    confirm_real_order, mark_real_unknown, public_real_order,
    reserve_real_order, settle_real_order, validated_real_stop_loss,
)
from .real_recovery import promote_stale_pending_to_unknown
from .restart_consistency import locked_real_consent_for_send
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


def reset_volatile_runtime_on_start():
    """Cold-start reset: persisted REAL opt-in never resumes a process worker."""
    global _reconcile_lock
    running.clear()
    reasons.clear()
    last_scan.clear()
    _user_locks.clear()
    _reconcile_lock=asyncio.Lock()


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


class RealStartInvalidated(Exception):
    """STOP advanced the durable generation after this START captured it."""


class RealStartConfigurationChanged(Exception):
    """The stored account/limits differ from the validated START request."""


def _f01_transaction_limits(sess):
    """Bound waits in PostgreSQL; no unbounded async event-loop DB blocking."""
    if sess.get_bind().dialect.name == 'postgresql':
        from sqlalchemy import text
        sess.execute(text("SET LOCAL lock_timeout = '1500ms'"))
        sess.execute(text("SET LOCAL statement_timeout = '4500ms'"))


def _f01_locked_global(sess, *, exclusive=False):
    """Global first: shared for per-user operations, exclusive for restart.

    PostgreSQL FOR SHARE permits independent users to proceed concurrently;
    restart FOR UPDATE waits for earlier START/STOP and prevents newer ones
    until the global invalidation and consent sweep commit atomically.
    """
    query=select(db.RealGlobalAuthorizationFence).where(
        db.RealGlobalAuthorizationFence.id==1)
    if sess.get_bind().dialect.name=='postgresql':
        query=query.with_for_update(read=not exclusive)
    epoch=sess.scalar(query)
    if epoch is None or epoch.generation is None or epoch.generation<0:
        raise RuntimeError('Migração de geração global REAL ausente; autorização bloqueada')
    return epoch


def _f01_locked_fence(sess, uid, *, global_locked=False, user_locked=False):
    """Strict lock order: global -> User -> user fence -> REAL consent.

    Callers that already hold global/User locks can opt out of taking them
    again. No method must acquire the global lock *after* consent.
    """
    if not global_locked:
        _f01_locked_global(sess)
    if not user_locked:
        found=sess.scalar(select(db.User.id).where(db.User.id==uid).with_for_update())
        if found is None:
            raise RuntimeError('Usuário não encontrado para coordenação REAL')
    fence=sess.scalar(select(db.RealAuthorizationFence).where(
        db.RealAuthorizationFence.user_id==uid).with_for_update())
    if fence is None:
        fence=db.RealAuthorizationFence(user_id=uid,generation=0)
        sess.add(fence)
        sess.flush()
    return fence


def capture_real_start_generation(uid):
    """Capture BOTH epochs before external checks; this grants no consent."""
    with db.SessionLocal() as sess:
        try:
            _f01_transaction_limits(sess)
            global_epoch=_f01_locked_global(sess)
            fence=_f01_locked_fence(sess,uid,global_locked=True)
            epochs=(global_epoch.generation,fence.generation)
            sess.commit()
            return epochs
        except Exception:
            sess.rollback()
            raise


def finalize_real_start_if_current(uid, expected_generation, account_id, stake, stop):
    """Fence check + consent write in ONE transaction protected by both epochs.

    The global shared lock bars restart while the per-user row lock serializes
    STOP, logout, admin revocation and security disarm. All paths fail closed
    when the required schema/epoch rows are missing.
    """
    with db.SessionLocal() as sess:
        try:
            _f01_transaction_limits(sess)
            global_epoch=_f01_locked_global(sess)
            fence=_f01_locked_fence(sess,uid,global_locked=True)
            if (global_epoch.generation,fence.generation)!=expected_generation:
                raise RealStartInvalidated('START antigo invalidado por revogação confirmada')
            profile=sess.get(db.AutomationProfile,uid)
            if (not profile or profile.account_mode!='real'
                or profile.real_account_id!=account_id):
                raise RealStartConfigurationChanged('Configuração REAL alterada; inicie novamente')
            latest=sess.get(db.AccountRiskSettings,(uid,'real',account_id))
            if (latest is None or Decimal(str(latest.stake))!=Decimal(str(stake))
                or Decimal(str(latest.stop_loss))!=Decimal(str(stop))):
                raise RealStartConfigurationChanged('Limites REAL alterados; revise preferências')
            consent=sess.scalar(select(db.RealRobotConsent).where(
                db.RealRobotConsent.user_id==uid).with_for_update())
            if consent is None:
                consent=db.RealRobotConsent(
                    user_id=uid,account_id=account_id,max_stake=str(stake),
                    stop_loss=str(stop))
                sess.add(consent)
            else:
                consent.account_id=account_id
                consent.max_stake=str(stake)
                consent.stop_loss=str(stop)
                consent.revoked_at=None
            sess.commit()
        except Exception:
            sess.rollback()
            raise


def revoke_real_consent_in_session(sess, uid, *, global_locked=False,
                                   user_locked=False):
    """Common revocation primitive; caller owns commit and rollback.

    The fence advances even on NO_CONSENT or ALREADY_REVOKED. The consent is
    only changed for the locked user and its currently stored REAL account.
    """
    _f01_transaction_limits(sess)
    if not global_locked:
        _f01_locked_global(sess)
    fence=_f01_locked_fence(sess,uid,global_locked=True,
                            user_locked=user_locked)
    consent=sess.scalar(select(db.RealRobotConsent).where(
        db.RealRobotConsent.user_id==uid).with_for_update())
    if consent is None:
        status,account_id,persisted='NO_CONSENT',None,None
    elif consent.revoked_at is not None:
        status,account_id,persisted='ALREADY_REVOKED',consent.account_id,True
    else:
        account_id=consent.account_id
        changed=sess.execute(update(db.RealRobotConsent).where(
            db.RealRobotConsent.user_id==uid,
            db.RealRobotConsent.account_id==account_id,
            db.RealRobotConsent.revoked_at.is_(None)
        ).values(revoked_at=db.utcnow()).execution_options(
            synchronize_session=False))
        if changed.rowcount!=1:
            raise RuntimeError('Revogação REAL não confirmada no banco')
        status,persisted='REVOKED_NOW',True
    fence.generation+=1
    return {'status':status,'account_id':account_id,
            'revocation_persisted':persisted}


def revoke_real_consent_for_stop(uid):
    """STOP/security: common transactional revocation, never just local state."""
    with db.SessionLocal() as sess:
        try:
            result=revoke_real_consent_in_session(sess,uid)
            sess.commit()
            return result
        except Exception:
            sess.rollback()
            raise


def disarm_real(uid,why):
    """Security disarm invalidates all older STARTs before local flag cleanup."""
    try:
        return revoke_real_consent_for_stop(uid)
    finally:
        # Local stop is still fail-closed if DB cannot be reached, but no
        # successful durable revocation is falsely reported.
        running.discard(uid)
        reasons[uid]=why


def disarm_revoked_real_workers():
    """Observe revocations committed by another service process.

    A local running set is never authoritative. Poll the shared database for
    previously started workers and clear stale process flags without broker I/O.
    The final send guard independently holds a PostgreSQL consent row lock.
    """
    users=tuple(running)
    if not users:
        return []
    with db.SessionLocal() as sess:
        consents={row.user_id:row for row in sess.scalars(
            select(db.RealRobotConsent).where(
                db.RealRobotConsent.user_id.in_(users))).all()}
        invalid=[uid for uid in users
                 if uid not in consents or consents[uid].revoked_at is not None]
    for uid in invalid:
        running.discard(uid)
        reasons[uid]='Autorização REAL revogada em outro processo; execução desligada'
    return invalid


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
            # An account-specific persisted REAL configuration is mandatory.
            # Never inherit DEMO values from the User compatibility columns.
            mode_settings=sess.get(db.AccountRiskSettings,
                                   (uid,'real',consent.account_id))
            if mode_settings is None:
                disarm_real(uid,'Configuração REAL da conta ausente; nenhuma compra permitida')
                return
            if mode_settings.martingale or mode_settings.cycles_limit:
                disarm_real(uid,'Progressões REAL não habilitadas')
                return
            try:
                validated_real_stop_loss(mode_settings, consent)
            except RealOrderGuardError:
                disarm_real(uid,'Stop Loss REAL diverge do consentimento; reautorize')
                return
            # Snapshot immutable values before broker network requests.
            account_id=consent.account_id
            stake=mode_settings.stake
            min_ratio=str(Decimal('1')+Decimal(profile.min_payout)/Decimal('100'))
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
        # P0-04: never trust the earlier consent snapshot after a reservation.
        # PostgreSQL FOR UPDATE serializes this final authorization check with
        # revocations by any other process. Keep the lock until the existing
        # outbound request finishes, or no new send can be guaranteed.
        broker_attempted=False
        consent_revoked=False
        skipped_before_send=False
        try:
            with locked_real_consent_for_send(uid,account_id) as authorized:
                if not authorized:
                    consent_revoked=True
                elif uid not in running or not real_available() or time.time()>deadline:
                    skipped_before_send=True
                else:
                    reasons[uid]='Solicitando contrato REAL à Deriv'
                    broker_attempted=True
                    receipt=await buy_real_contract(
                        token,account_id,symbol=candidate['symbol'],
                        contract_type=candidate['direction'],stake=stake,
                        duration=expiry,currency='USD',
                        min_payout_ratio=min_ratio,
                        latest_buy_send_epoch=deadline)
            if consent_revoked:
                # The newly reserved PENDING row remains untouched and blocks
                # reentry until conservative reconciliation or manual review.
                running.discard(uid)
                reasons[uid]='Autorização REAL revogada antes do envio; reserva preservada'
                return
            if skipped_before_send:
                with db.SessionLocal() as sess:
                    order=sess.get(db.RealOrder,order_id)
                    cancel_unsubmitted_real_order(sess,order,'Desligado ou expirado antes do envio')
                return
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
            if broker_attempted:
                # The broker might have received the buy. Never assume no trade.
                with db.SessionLocal() as sess:
                    mark_real_unknown(sess,sess.get(db.RealOrder,order_id))
                disarm_real(uid,'Erro REAL incerto; confira contratos na Deriv')
            else:
                # The final database authorization itself failed. No request
                # was sent; preserve the reservation for manual inspection.
                running.discard(uid)
                reasons[uid]='Não foi possível confirmar autorização REAL; envio bloqueado'
            logger.error('REAL execution uncertain: %s',type(exc).__name__)


async def real_tick():
    # A different Render worker can revoke our persisted consent; synchronize
    # the local status before considering any new scan or dispatch.
    disarm_revoked_real_workers()
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
    # P0-02: local-only quarantine of stale PENDING records before existing
    # read-only broker status reconciliation. Does not create, retry or release
    # any contract. UNKNOWN with no contract ID stays blocked for review.
    with db.SessionLocal() as sess:
        promote_stale_pending_to_unknown(sess)
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

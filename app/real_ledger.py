"""Durable, account-scoped REAL order ledger. Never touches DEMO tables."""
from __future__ import annotations

import re
from datetime import timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from . import db
from .demo_ledger import official_broker_epoch


ACTIVE = ('PENDING', 'CONFIRMED', 'UNKNOWN')


class RealOrderGuardError(Exception):
    pass


def _money(value, label):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RealOrderGuardError(label + ' inválido') from exc
    if not result.is_finite():
        raise RealOrderGuardError(label + ' inválido')
    return result


def _risk_check(orders, stake, stop_loss, max_stake, stop_win='0'):
    # Daily limits roll over at midnight America/Sao_Paulo, but unresolved
    # contracts from a previous day still block new REAL buys.
    local_zone=ZoneInfo('America/Sao_Paulo')
    current_day=db.utcnow().astimezone(local_zone).date()
    amount = _money(stake, 'Valor da entrada')
    cap = _money(max_stake, 'Limite por entrada')
    stop = _money(stop_loss, 'Stop Loss')
    if amount < Decimal('0.50') or cap < Decimal('0.50') or amount > cap:
        raise RealOrderGuardError('Valor de entrada REAL fora do limite autorizado')
    if stop <= 0:
        raise RealOrderGuardError('Stop Loss REAL obrigatório')
    if any(row.state in ACTIVE for row in orders):
        raise RealOrderGuardError('Conta REAL com contrato aberto ou incerto')
    def same_local_day(order):
        instant=order.broker_purchase_at or order.created_at
        if instant is None:
            return False
        if instant.tzinfo is None:
            instant=instant.replace(tzinfo=timezone.utc)
        return instant.astimezone(local_zone).date()==current_day
    pnl = sum((_money(row.pnl, 'Resultado')
               for row in orders if row.state == 'SETTLED'
               and row.pnl is not None and same_local_day(row)), Decimal('0'))
    win = _money(stop_win, 'Stop Win')
    if win < 0:
        raise RealOrderGuardError('Stop Win inválido')
    if win > 0 and pnl >= win:
        raise RealOrderGuardError('Stop Win REAL atingido')
    # Only the user's selected stop applies; no arbitrary server daily cap.
    if pnl <= -stop or pnl - amount < -stop:
        raise RealOrderGuardError('Stop Loss REAL atingido ou exposição ultrapassaria o limite')
    return amount


def reserve_real_order(session, user, *, account_id, request_key,
                       symbol, direction, duration, stake, currency):
    """Reserve before any broker I/O; user row lock serializes concurrent requests.

    Account ID must already be validated from the authenticated Deriv account
    list. This function additionally validates persisted opt-in against it.
    """
    if not isinstance(account_id, str) or not re.fullmatch(r'[A-Za-z0-9]{2,100}',account_id):
        raise RealOrderGuardError('Conta REAL inválida')
    if (not isinstance(request_key, str) or
        not re.fullmatch(r'[A-Za-z0-9_-]{8,64}', request_key)):
        raise RealOrderGuardError('Chave de tentativa REAL inválida')
    if not isinstance(symbol,str) or not re.fullmatch(r'[A-Za-z0-9_]{2,30}',symbol):
        raise RealOrderGuardError('Ativo REAL inválido')
    if direction not in ('CALL','PUT') or duration not in (1,3,5,15):
        raise RealOrderGuardError('Tipo ou expiração REAL inválida')
    if not isinstance(currency,str) or not re.fullmatch(r'[A-Za-z0-9]{2,20}',currency):
        raise RealOrderGuardError('Moeda REAL inválida')
    locked = session.get(db.User, user.id, with_for_update=True)
    if locked is None:
        raise RealOrderGuardError('Usuário inválido')
    consent = session.get(db.RealRobotConsent, user.id)
    if (consent is None or consent.revoked_at is not None
        or consent.account_id != account_id):
        raise RealOrderGuardError('Autorização REAL ausente ou revogada')
    previous = session.scalar(select(db.RealOrder).where(
        db.RealOrder.account_id == account_id,
        db.RealOrder.request_key == request_key))
    if previous is not None:
        return previous, False
    orders = session.scalars(select(db.RealOrder).where(
        db.RealOrder.account_id == account_id)).all()
    amount = _risk_check(orders, stake, consent.stop_loss, consent.max_stake, locked.stop_win)
    order = db.RealOrder(
        user_id=user.id, account_id=account_id, request_key=request_key,
        active_slot=account_id, state='PENDING', symbol=symbol,
        direction=direction, duration_minutes=duration,
        stake=str(amount), currency=currency)
    session.add(order)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise RealOrderGuardError('Conta REAL já tem uma compra reservada') from exc
    session.refresh(order)
    return order, True


def confirm_real_order(session, order, receipt):
    if order.state != 'PENDING' or receipt.get('account_mode') != 'real':
        raise RealOrderGuardError('Confirmação REAL inválida')
    contract_id = str(receipt.get('contract_id',''))
    if not contract_id.isdigit():
        raise RealOrderGuardError('Contrato REAL não confirmado')
    price = _money(receipt.get('buy_price'), 'Preço REAL')
    if price <= 0 or price > _money(order.stake, 'Valor da entrada'):
        raise RealOrderGuardError('Preço de compra REAL ultrapassou o autorizado')
    order.contract_id=contract_id
    order.buy_price=str(price)
    order.broker_purchase_at=official_broker_epoch(receipt.get('broker_purchase_time'))
    order.state='CONFIRMED'
    session.commit()



def cancel_unsubmitted_real_order(session, order, reason):
    """Release reservation only when broker buy was provably never dispatched."""
    if order.state != 'PENDING':
        raise RealOrderGuardError('Somente tentativa não enviada pode ser cancelada')
    order.state='CANCELLED'
    order.error_summary=str(reason)[:200]
    order.active_slot=None
    session.commit()


def mark_real_unknown(session, order):
    if order.state=='PENDING':
        order.state='UNKNOWN'
        order.error_summary='Resultado REAL incerto: reconciliação obrigatória'
        session.commit()


def settle_real_order(session, order, contract):
    if order.state not in ACTIVE or not order.contract_id:
        raise RealOrderGuardError('Contrato REAL não reconciliável')
    if str(contract.get('contract_id','')) != order.contract_id:
        raise RealOrderGuardError('Contrato da Deriv não corresponde à operação REAL')
    if contract.get('is_sold') not in (True,1,'1'):
        return False
    profit=_money(contract.get('profit'),'Lucro ou prejuízo REAL')
    order.pnl=str(profit)
    order.broker_status=str(contract.get('status','settled'))[:40]
    order.state='SETTLED'
    order.active_slot=None
    session.commit()
    return True


def public_real_order(order):
    return {
        'id':order.id,'state':order.state,'symbol':order.symbol,
        'direction':order.direction,'stake':order.stake,'currency':order.currency,
        'duration_minutes':order.duration_minutes,'buy_price':order.buy_price,
        'pnl':order.pnl,'broker_status':order.broker_status,
        'contract_id':order.contract_id if order.state!='PENDING' else None,
        'account_mode':'real',
        'created_at':order.created_at.isoformat() if order.created_at else None,
    }

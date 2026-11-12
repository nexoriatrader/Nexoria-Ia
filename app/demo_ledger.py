"""Fail-closed, demo-only order ledger. No access to real account execution."""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from . import db

ACTIVE = {'PENDING', 'CONFIRMED', 'UNKNOWN'}

class OrderGuardError(Exception):
    pass

def money(s):
    try:
        d=Decimal(str(s))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise OrderGuardError('Valor financeiro inválido') from exc
    if not d.is_finite():
        raise OrderGuardError('Valor financeiro inválido')
    return d

def public_order(o):
    return {'id':o.id,'state':o.state,'contract_id':o.contract_id,
            'symbol':o.symbol,'direction':o.direction,'stake':o.stake,
            'buy_price':o.buy_price,'pnl':o.pnl,'broker_status':o.broker_status,
            'created_at':o.created_at.isoformat() if o.created_at else None}

def order_guard(user, orders, stake):
    """Use only settled demo broker PnL; reject negative-edge and exposed entries."""
    if any(o.state in ACTIVE for o in orders):
        raise OrderGuardError('Contrato demo em andamento ou pendente de reconciliação')
    # Cumulative demo ledger, conservatively never reset just by reconnecting.
    pnl=sum((money(o.pnl) for o in orders if o.state=='SETTLED' and o.pnl is not None),Decimal('0'))
    stake=money(stake)
    if stake < Decimal('2') or stake > Decimal('1000'):
        raise OrderGuardError('Valor da entrada fora dos limites de treinamento')
    stop_win=money(user.stop_win)
    stop_loss=money(user.stop_loss)
    if stop_loss <= 0 or stop_win <= 0:
        raise OrderGuardError('Stops inválidos')
    if pnl >= stop_win:
        raise OrderGuardError('Stop Win atingido')
    if pnl <= -stop_loss or pnl-stake < -stop_loss:
        raise OrderGuardError('A próxima entrada excederia o Stop Loss')
    # Execution with Martingale progression is deliberately not supported in v0.5.
    if user.martingale:
        raise OrderGuardError('Martingale indisponível para compras demo até validação adicional')
    return pnl

def reserve(session, user, *, request_key, symbol, direction, minutes):
    if not isinstance(request_key,str) or not 8<=len(request_key)<=64 or not all(c.isalnum() or c in '-_' for c in request_key):
        raise OrderGuardError('Identificador de tentativa inválido')
    if direction not in {'CALL','PUT'} or minutes not in {1,3,5}:
        raise OrderGuardError('Contrato demo inválido')
    if not isinstance(symbol,str) or not symbol.isalnum() or not 2<=len(symbol)<=30:
        raise OrderGuardError('Ativo demo inválido')
    previous=session.scalar(select(db.DemoOrder).where(db.DemoOrder.user_id==user.id,
                                                      db.DemoOrder.request_key==request_key))
    if previous:
        # Idempotent retry MUST NOT trigger another broker request.
        return previous, False
    entries=session.scalars(select(db.DemoOrder).where(db.DemoOrder.user_id==user.id)).all()
    order_guard(user,entries,user.stake)
    order=db.DemoOrder(user_id=user.id,request_key=request_key,active_slot=user.id,
        state='PENDING',symbol=symbol,direction=direction,duration_minutes=minutes,stake=user.stake)
    session.add(order)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise OrderGuardError('Outra tentativa já está registrada para esta conta') from exc
    session.refresh(order)
    return order, True

def confirmed(session, order, receipt):
    if order.state!='PENDING':
        raise OrderGuardError('Estado de compra inesperado')
    order.contract_id=str(receipt['contract_id'])
    order.buy_price=str(receipt['buy_price'])
    order.state='CONFIRMED'
    session.commit()

def unknown(session, order):
    if order.state=='PENDING':
        order.state='UNKNOWN'
        order.error_summary='Confirmação indisponível; requer reconciliação com a Deriv'
        session.commit()

def settle(session, order, broker_contract):
    """Never fabricate settlement; only broker is_sold and finite profit accepted."""
    if order.state not in ACTIVE or not order.contract_id:
        raise OrderGuardError('Contrato sem confirmação para liquidar')
    if str(broker_contract.get('contract_id',''))!=order.contract_id:
        raise OrderGuardError('Identificador retornado não corresponde ao contrato')
    sold=broker_contract.get('is_sold')
    if sold not in (True, 1, '1'):
        return False
    profit=money(broker_contract.get('profit'))
    order.pnl=str(profit)
    order.broker_status=str(broker_contract.get('status','settled'))[:40]
    order.state='SETTLED'
    order.active_slot=None
    session.commit()
    return True

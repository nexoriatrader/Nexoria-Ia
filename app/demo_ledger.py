"""Fail-closed, demo-only order ledger. No access to real account execution."""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
import hashlib
import re
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

def order_guard(user, orders, stake, *, after_order_id=0,
                parallel=False, symbol=None):
    """Official DEMO risk guard, including exposure reserved in open contracts."""
    active=[o for o in orders if o.state in ACTIVE]
    stake=money(stake)
    if parallel:
        if any(o.state in ('PENDING','UNKNOWN') for o in active):
            raise OrderGuardError('Contrato pendente ou incerto; novas entradas bloqueadas')
        if any(o.symbol==symbol for o in active):
            raise OrderGuardError('Já existe contrato aberto neste ativo')
        if any(o.active_slot==user.id for o in active):
            raise OrderGuardError('Contrato antigo exige liquidação antes do modo paralelo')
    elif active:
        raise OrderGuardError('Contrato demo em andamento ou pendente de reconciliação')
    pnl=sum((money(o.pnl) for o in orders if o.state=='SETTLED' and o.pnl is not None
             and (after_order_id==0 or (getattr(o,'id',None) or 0)>after_order_id)),Decimal('0'))
    if stake < Decimal('0.50') or stake > Decimal('1000'):
        raise OrderGuardError('Valor da entrada fora dos limites de treinamento')
    stop_win=money(user.stop_win)
    stop_loss=money(user.stop_loss)
    if stop_loss<0 or stop_win<0:
        raise OrderGuardError('Stops não podem ser negativos')
    # Exposure is still considered by an explicitly configured Stop Loss,
    # but a fixed global ceiling is not imposed on DEMO experiments.
    exposure=sum((money(o.stake) for o in active),Decimal('0'))
    if stop_win>0 and pnl>=stop_win:
        raise OrderGuardError('Stop Win atingido')
    if stop_loss>0 and (pnl<=-stop_loss or pnl-exposure-stake < -stop_loss):
        raise OrderGuardError('A exposição máxima excederia o Stop Loss')
    if user.martingale:
        raise OrderGuardError('Martingale indisponível para compras demo na configuração antiga')
    return pnl


def _symbol_slot(user_id:int, symbol:str)->int:
    # Stable, account-and-symbol-specific slot; 63-bit positive SQLite/Postgres integer.
    digest=hashlib.sha256(f'{user_id}:{symbol}'.encode('ascii')).digest()
    return (int.from_bytes(digest[:8],'big') & ((1<<63)-1)) or 1


def reserve(session, user, *, request_key, symbol, direction, minutes,
            stake_override=None, parallel=False):
    if not isinstance(request_key,str) or not 8<=len(request_key)<=64 or not all(c.isalnum() or c in '-_' for c in request_key):
        raise OrderGuardError('Identificador de tentativa inválido')
    if direction not in {'CALL','PUT'} or minutes not in {1,3,5,15}:
        raise OrderGuardError('Contrato demo inválido')
    if not isinstance(symbol,str) or not re.fullmatch(r'[A-Za-z0-9_]{2,30}',symbol):
        raise OrderGuardError('Ativo demo inválido')
    # PostgreSQL user-row lock serializes risk/exposure checks between workers.
    # Same-process autopilot_lock independently serializes automatic activations.
    locked_user=session.get(db.User,user.id,with_for_update=True)
    if not locked_user:
        raise OrderGuardError('Conta DEMO indisponível')
    previous=session.scalar(select(db.DemoOrder).where(
        db.DemoOrder.user_id==user.id,db.DemoOrder.request_key==request_key))
    if previous:
        return previous,False
    entries=session.scalars(select(db.DemoOrder).where(
        db.DemoOrder.user_id==user.id)).all()
    chosen_stake=user.stake if stake_override is None else str(stake_override)
    boundary=session.scalars(select(db.RiskSession).where(
        db.RiskSession.user_id==user.id).order_by(db.RiskSession.id.desc()).limit(1)).first()
    order_guard(locked_user,entries,chosen_stake,
                after_order_id=boundary.after_order_id if boundary else 0,
                parallel=parallel,symbol=symbol)
    slot=_symbol_slot(user.id,symbol) if parallel else user.id
    order=db.DemoOrder(user_id=user.id,request_key=request_key,active_slot=slot,
        state='PENDING',symbol=symbol,direction=direction,duration_minutes=minutes,stake=chosen_stake)
    session.add(order)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise OrderGuardError('Outra tentativa já está registrada para esta conta ou ativo') from exc
    session.refresh(order)
    return order,True


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

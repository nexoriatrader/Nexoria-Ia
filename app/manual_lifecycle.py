"""Durable lifecycle transitions for Nexora Manual signal queues.

This module only changes ManualSignal/ManualSignalRun metadata. Broker orders
remain owned by the official DEMO and REAL ledgers and are never deleted here.
"""
from __future__ import annotations

from sqlalchemy import select

from . import db

QUEUE_STATUSES = ("SCHEDULED", "CLAIMED", "UNKNOWN")
IN_FLIGHT_SIGNAL_STATUSES = ("CLAIMED", "UNKNOWN")
ACTIVE_LEDGER_STATUSES = ("PENDING", "CONFIRMED", "UNKNOWN")


def list_session_signals(session, run):
    if run is None:
        return []
    return session.scalars(select(db.ManualSignal).where(
        db.ManualSignal.user_id == run.user_id,
        db.ManualSignal.session_number == run.session_number,
    ).order_by(db.ManualSignal.scheduled_at, db.ManualSignal.id)).all()


def queue_session_signals(session, run):
    if run is None:
        return []
    return session.scalars(select(db.ManualSignal).where(
        db.ManualSignal.user_id == run.user_id,
        db.ManualSignal.session_number == run.session_number,
        db.ManualSignal.status.in_(QUEUE_STATUSES),
    ).order_by(db.ManualSignal.scheduled_at, db.ManualSignal.id)).all()


def _signal_order(session, run, signal):
    mode = signal.ledger_mode or run.account_mode
    if mode == "real":
        if signal.ledger_order_id:
            return session.get(db.RealOrder, signal.ledger_order_id)
        return session.scalar(select(db.RealOrder).where(
            db.RealOrder.user_id == run.user_id,
            db.RealOrder.request_key == signal.request_key,
        ))
    if signal.ledger_order_id:
        return session.get(db.DemoOrder, signal.ledger_order_id)
    return session.scalar(select(db.DemoOrder).where(
        db.DemoOrder.user_id == run.user_id,
        db.DemoOrder.request_key == signal.request_key,
    ))


def _active_ledger_exists(session, run):
    if run.account_mode == "real":
        query = select(db.RealOrder.id).where(
            db.RealOrder.user_id == run.user_id,
            db.RealOrder.state.in_(ACTIVE_LEDGER_STATUSES),
        )
        if run.real_account_id:
            query = query.where(db.RealOrder.account_id == run.real_account_id)
        return session.scalar(query.limit(1)) is not None
    return session.scalar(select(db.DemoOrder.id).where(
        db.DemoOrder.user_id == run.user_id,
        db.DemoOrder.state.in_(ACTIVE_LEDGER_STATUSES),
    ).limit(1)) is not None


def complete_exhausted_manual_run(session, run):
    """Mark an exhausted schedule complete; active contracts remain in the ledger."""
    if run is None or run.status not in ("RUNNING", "PAUSED"):
        return False
    signals = list_session_signals(session, run)
    if not signals or any(signal.status in QUEUE_STATUSES for signal in signals):
        return False
    run.status = "COMPLETED"
    run.completed_at = db.utcnow()
    run.pause_reason = "Lista concluída; robô desligado automaticamente"
    return True


def finish_manual_cancellation_if_safe(session, run):
    """Cancel only future/unsent queue rows after active or uncertain work is clear.

    Callers serialize this transition with the corresponding execution lock. An
    UNKNOWN attempt is never guessed away: it must resolve in the official ledger.
    """
    if run is None or run.status != "CANCELLING":
        return False
    if _active_ledger_exists(session, run):
        return False

    signals = list_session_signals(session, run)
    for signal in signals:
        if signal.status == "SCHEDULED":
            signal.status = "CANCELLED"
            signal.reason = "Desagendado pelo usuário; nenhuma compra foi enviada"
            signal.capacity_slot = None
            continue
        if signal.status not in IN_FLIGHT_SIGNAL_STATUSES:
            continue

        order = _signal_order(session, run, signal)
        if order is None:
            if signal.status == "CLAIMED":
                # Safe only because the caller holds the execution lock: no broker
                # request can still be in flight for this claim.
                signal.status = "CANCELLED"
                signal.reason = "Tentativa não enviada; desagendada pelo usuário"
                signal.capacity_slot = None
                continue
            return False
        if order.state in ACTIVE_LEDGER_STATUSES:
            return False
        if order.state == "SETTLED":
            signal.status = "COMPLETED"
            signal.reason = "Liquidação oficial confirmada"
            signal.capacity_slot = None
            continue
        if order.state == "CANCELLED":
            signal.status = "FAILED"
            signal.reason = "Ledger confirmou que nenhuma compra permaneceu ativa"
            signal.capacity_slot = None
            continue
        return False

    if any(signal.status in IN_FLIGHT_SIGNAL_STATUSES for signal in signals):
        return False
    run.status = "COMPLETED"
    run.completed_at = db.utcnow()
    run.pause_reason = "Lista desagendada; contratos e resultados oficiais preservados"
    return True

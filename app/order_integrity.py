"""P0-03: database-only integrity checks; never issues or authorizes orders.

Pure comparisons and read-only audits. These helpers deliberately return
conflicts rather than manufacturing settlement/cancellation evidence.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from sqlalchemy import select
from . import db

ACTIVE_STATES = frozenset(("PENDING", "CONFIRMED", "UNKNOWN"))
TERMINAL_STATES = frozenset(("SETTLED", "CANCELLED"))


def _same_amount(first, second) -> bool:
    try:
        left, right = Decimal(str(first)), Decimal(str(second))
    except (InvalidOperation, ValueError, TypeError):
        return False
    return left.is_finite() and right.is_finite() and left == right


def real_replay_matches(previous, *, user_id, account_id, symbol, direction,
                        duration, stake, currency) -> bool:
    """Idempotency means the same request, not merely the same request key."""
    return (
        previous.user_id == user_id and previous.account_id == account_id
        and previous.symbol == symbol and previous.direction == direction
        and previous.duration_minutes == duration
        and _same_amount(previous.stake, stake)
        and previous.currency == currency
    )


def demo_replay_matches(previous, *, user_id, symbol, direction, minutes,
                        stake_override=None) -> bool:
    """An omitted stake override reuses legacy DEMO request semantics."""
    return (
        previous.user_id == user_id and previous.symbol == symbol
        and previous.direction == direction
        and previous.duration_minutes == minutes
        and (stake_override is None or _same_amount(previous.stake, stake_override))
    )


def audit_real_account(session, user_id: int, account_id: str) -> dict:
    """Read-only, authenticated-user-scoped invariant report.

    Returns issue counts only (no broker tokens or contract details).
    Not a trading permission signal, regardless of whether issues are found.
    """
    rows = session.scalars(select(db.RealOrder).where(
        db.RealOrder.user_id == user_id,
        db.RealOrder.account_id == account_id,
    ).order_by(db.RealOrder.id.asc())).all()

    issues: dict[str, int] = {}
    def flag(reason):
        issues[reason] = issues.get(reason, 0) + 1

    active_count = 0
    keys = set()
    contracts = set()
    for order in rows:
        active = order.state in ACTIVE_STATES
        if active:
            active_count += 1
            if order.active_slot != account_id:
                flag("ACTIVE_SLOT_INCONSISTENT")
        elif order.active_slot is not None:
            flag("TERMINAL_SLOT_STILL_HELD")
        if order.state not in ACTIVE_STATES | TERMINAL_STATES:
            flag("UNKNOWN_LEDGER_STATE")
        if order.state == "CONFIRMED" and not order.contract_id:
            flag("CONFIRMED_WITHOUT_CONTRACT_ID")
        if order.state == "SETTLED" and (not order.contract_id or order.pnl is None):
            flag("SETTLED_WITHOUT_EVIDENCE")
        if order.request_key in keys:
            flag("DUPLICATE_REQUEST_KEY")
        keys.add(order.request_key)
        if order.contract_id:
            if order.contract_id in contracts:
                flag("DUPLICATE_CONTRACT_ID")
            contracts.add(order.contract_id)
    if active_count > 1:
        flag("MULTIPLE_ACTIVE_RECORDS")
    return {
        "account_id": account_id,
        "records_checked": len(rows),
        "active_count": active_count,
        "issue_count": sum(issues.values()),
        "issues": issues,
        "requires_review": bool(issues),
    }

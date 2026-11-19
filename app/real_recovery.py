"""P0-02 — Conservative local recovery of unresolved REAL ledger records.

This module performs **database-only** state inspection and a conservative
PENDING -> UNKNOWN transition. It NEVER imports a broker, fetches credentials,
submits an order, fabricates a contract ID, settles a trade or releases a slot.

Without official evidence that a buy was never sent, an old PENDING record
remains an active obligation. Unknown outcomes without contract IDs MUST be
reviewed with independent official evidence; age alone cannot clear them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from . import db

ACTIVE_REAL = frozenset(("PENDING", "UNKNOWN", "CONFIRMED"))
DEFAULT_PENDING_GRACE_SECONDS = 15 * 60
MAX_RECOVERY_BATCH = 100
RECOVERY_NOTE = "Reserva antiga sem confirmação: resultado desconhecido; revisão obrigatória"


def _utc(timestamp: datetime | None) -> datetime | None:
    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def classify_unresolved_real_order(order) -> dict:
    """Return a display-only diagnosis. A diagnosis can NEVER release a slot."""
    active = order.state in ACTIVE_REAL
    known_contract = bool(order.contract_id)
    slot_held = bool(order.active_slot)
    inconsistent_slot = (active and order.active_slot != order.account_id) or (
        not active and slot_held
    )
    if inconsistent_slot:
        status = "INTEGRITY_REVIEW"
    elif order.state == "PENDING":
        status = "AWAITING_CONFIRMATION"
    elif order.state == "UNKNOWN" and not known_contract:
        status = "MANUAL_REVIEW_NO_CONTRACT"
    elif order.state == "CONFIRMED" and not known_contract:
        status = "INTEGRITY_REVIEW"
    elif order.state in ("UNKNOWN", "CONFIRMED"):
        status = "AWAITING_OFFICIAL_SETTLEMENT"
    elif order.state in ("SETTLED", "CANCELLED"):
        status = "CLOSED"
    else:
        status = "INTEGRITY_REVIEW"
    return {
        "status": status,
        "needs_manual_review": status in ("MANUAL_REVIEW_NO_CONTRACT", "INTEGRITY_REVIEW"),
        "account_blocked": active or inconsistent_slot,
        "slot_held": slot_held,
        "contract_id_known": known_contract,
        "safe_to_retry": False if active or inconsistent_slot else None,
    }


def promote_stale_pending_to_unknown(session, *, now: datetime | None = None,
                                     grace_seconds: int = DEFAULT_PENDING_GRACE_SECONDS,
                                     batch_size: int = MAX_RECOVERY_BATCH) -> list[int]:
    """Mark stale unresolved reservations UNKNOWN while preserving their slot.

    Single conditional UPDATE per record prevents an outdated background sweep
    from overwriting a concurrent valid confirmation/cancellation. Never
    modifies PENDING records with a missing/mismatched slot: those require an
    integrity review. No broker calls, retries, cancellations or settlements.

    Call with an isolated SQLAlchemy Session. The caller owns its transaction;
    successful transitions are committed once, and a rollback leaves no edits.
    """
    if not isinstance(grace_seconds, int) or grace_seconds <= 0:
        raise ValueError("grace_seconds must be a positive integer")
    if not isinstance(batch_size, int) or not 1 <= batch_size <= MAX_RECOVERY_BATCH:
        raise ValueError("batch_size must be within 1..100")
    current = _utc(now) if now is not None else db.utcnow()
    cutoff = current - timedelta(seconds=grace_seconds)
    candidates = session.scalars(select(db.RealOrder).where(
        db.RealOrder.state == "PENDING",
        db.RealOrder.updated_at <= cutoff,
        db.RealOrder.active_slot == db.RealOrder.account_id,
    ).order_by(db.RealOrder.updated_at.asc(), db.RealOrder.id.asc())
      .limit(batch_size)).all()

    changed: list[int] = []
    try:
        for order in candidates:
            # Conditional compare-and-swap: do not overwrite a concurrent state
            # transition, last-update or active-slot change.
            result = session.execute(update(db.RealOrder).where(
                db.RealOrder.id == order.id,
                db.RealOrder.state == "PENDING",
                db.RealOrder.updated_at <= cutoff,
                db.RealOrder.active_slot == db.RealOrder.account_id,
            ).values(state="UNKNOWN", error_summary=RECOVERY_NOTE,
                     updated_at=current))
            if result.rowcount == 1:
                changed.append(order.id)
        session.commit()
        # SQL UPDATE bypasses ORM identity-map synchronization on some
        # drivers; callers must not observe a stale PENDING object.
        session.expire_all()
    except Exception:
        session.rollback()
        raise
    return changed


def account_recovery_summary(session, user_id: int, account_id: str | None) -> dict:
    """Read-only, user/account-scoped summary; no financial access or effects."""
    if not account_id:
        return {"account_id": None, "active_count": 0, "pending_count": 0,
                "unknown_count": 0, "review_count": 0, "blocked": False,
                "reason": "Selecione uma conta para consultar suas pendências"}
    rows = session.scalars(select(db.RealOrder).where(
        db.RealOrder.user_id == user_id,
        db.RealOrder.account_id == account_id,
        db.RealOrder.state.in_(("PENDING", "UNKNOWN", "CONFIRMED")),
    )).all()
    classified = [classify_unresolved_real_order(row) for row in rows]
    blocked = any(x["account_blocked"] for x in classified)
    return {
        "account_id": account_id,
        "active_count": len(rows),
        "pending_count": sum(o.state == "PENDING" for o in rows),
        "unknown_count": sum(o.state == "UNKNOWN" for o in rows),
        "review_count": sum(x["needs_manual_review"] for x in classified),
        "blocked": blocked,
        "reason": ("Há operações sem conclusão confirmada; novas ordens permanecem bloqueadas"
                   if blocked else "Nenhuma operação REAL pendente nesta conta"),
    }

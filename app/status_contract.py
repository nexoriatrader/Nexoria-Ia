"""P0-05 — Read-only, conservative operational status contract.

Status is NOT an activation command or evidence of a broker order.
No broker imports, tokens, requests, writes, consent changes or worker starts.

No distributed worker registry exists in the current schema. A request handled
by one process cannot rule out workers in another process; therefore absence
of local evidence is UNKNOWN, never an invented global "stopped" assertion.
"""
from __future__ import annotations

from datetime import timezone
from sqlalchemy import select

from . import db


LIFECYCLE = {
    "PENDING": "RESERVED_UNCONFIRMED",
    "UNKNOWN": "UNKNOWN_UNRESOLVED",
    "CONFIRMED": "CONFIRMED_OPEN_UNSETTLED",
    "SETTLED": "SETTLED",
    "CANCELLED": "CANCELLED",
}
UNSETTLED = frozenset(("PENDING", "UNKNOWN", "CONFIRMED"))


def contract_stage(state: str) -> str:
    """CONFIRMED is the purchase confirmation, NOT a settled result."""
    return LIFECYCLE.get(state, "UNKNOWN_UNVERIFIED")


def contract_summary(session, user_id: int, *, account_mode: str,
                     account_id: str | None) -> dict:
    """Read-only; never substitute another REAL account when selection is absent."""
    if account_mode not in ("demo", "real"):
        return {"known": False, "reason": "Modalidade não identificada",
                "open_unsettled": None, "counts": None, "latest_stage": None}
    if not account_id:
        return {"known": False, "reason": "Conta não selecionada",
                "open_unsettled": None, "counts": None, "latest_stage": None}
    table = db.RealOrder if account_mode == "real" else db.DemoOrder
    clauses = [table.user_id == user_id]
    if account_mode == "real":
        clauses.append(table.account_id == account_id)
    # This summary is not the historical ledger; read states only.
    rows = session.scalars(
        select(table).where(*clauses).order_by(table.id.desc())
    ).all()
    counts = {state: 0 for state in LIFECYCLE}
    invalid_count = 0
    for row in rows:
        if row.state in counts:
            counts[row.state] += 1
        else:
            invalid_count += 1
    pending = sum(counts[state] for state in UNSETTLED)
    if invalid_count:
        overall = "UNKNOWN_UNVERIFIED"
    elif counts["UNKNOWN"]:
        overall = "UNKNOWN_UNRESOLVED"
    elif counts["PENDING"]:
        overall = "RESERVED_UNCONFIRMED"
    elif counts["CONFIRMED"]:
        overall = "CONFIRMED_OPEN_UNSETTLED"
    else:
        overall = "NO_OPEN_CONTRACT_RECORDED"
    return {
        "known": invalid_count == 0,
        "reason": ("Estados de contrato não reconhecidos; conferência necessária"
                   if invalid_count else None),
        "counts": {**counts, "UNRECOGNIZED": invalid_count},
        "open_unsettled": pending if not invalid_count else None,
        "requires_reconciliation": bool(pending or invalid_count),
        "overall_stage": overall,
        "latest_stage": contract_stage(rows[0].state) if rows else None,
        # A confirmed purchase must remain open until official settlement.
        "confirmed_open_count": counts["CONFIRMED"],
        "settled_count": counts["SETTLED"],
    }


def task_is_alive(task) -> bool:
    """A process-local scheduler task is evidence only for this process."""
    try:
        return task is not None and not task.done()
    except (AttributeError, RuntimeError):
        return False


def running_evidence(*, local_running: bool, worker_alive: bool,
                     persisted_authorized: bool,
                     execution_gate: bool, account_selected: bool) -> dict:
    """Tri-state global claim: True, False (provably gated), or None (unknown).

    Even if a process has local running=True, it is not a running worker
    without a live scheduler task and durable authorization. Without an
    external registry, local_running=False is not proof other workers stopped.
    """
    if not execution_gate:
        now, reason = False, "EXECUTION_GATE_DISABLED"
    elif not persisted_authorized:
        now, reason = False, "NO_PERSISTED_AUTHORIZATION"
    elif not account_selected:
        now, reason = False, "NO_SELECTED_ACCOUNT"
    elif local_running and worker_alive:
        now, reason = True, "LOCAL_WORKER_AND_PERSISTED_AUTHORIZATION"
    else:
        now, reason = None, "OTHER_PROCESS_OR_WORKER_UNVERIFIED"
    return {
        "running_now": now,
        "running_evidence": reason,
        "running_state": "RUNNING" if now is True else
                         "STOPPED_OR_BLOCKED" if now is False else "UNKNOWN",
        "state_stale": now is None,
        "scope": "LOCAL_PROCESS_PLUS_DATABASE",
    }


def evaluate_user_status(session, user, *, profile, local_demo: bool,
                         local_real: bool, demo_scheduler, real_scheduler,
                         demo_gate: bool, real_gate: bool,
                         real_feature_gate: bool,
                         real_release_approved: bool) -> dict:
    """Gather independent configuration, gates, consent and runtime evidence.

    Never reads OAuth tokens. No DB mutations. Server date is informational
    only: it cannot be used to prove a worker exists on another process.
    """
    configured_mode = profile.account_mode if profile else "demo"
    configured = profile is not None
    selected_real = (profile.real_account_id if profile
                     and configured_mode == "real" else None)
    selected_demo = user.deriv_account_id
    real_consent = session.get(db.RealRobotConsent, user.id)
    real_authorized = bool(real_consent and selected_real
        and real_consent.account_id == selected_real
        and real_consent.revoked_at is None)
    demo_run = session.get(db.RobotRun, user.id)
    demo_consent = session.get(db.AutoPilotResumeConsent, user.id)
    demo_authorized = bool(
        demo_run is not None and demo_consent is not None
        and user.selected_mode == "demo"
        and (profile is None or configured_mode == "demo")
    )
    demo_runtime = running_evidence(
        local_running=local_demo,worker_alive=task_is_alive(demo_scheduler),
        persisted_authorized=demo_authorized,execution_gate=demo_gate,
        account_selected=bool(selected_demo)
    )
    real_runtime = running_evidence(
        local_running=local_real,worker_alive=task_is_alive(real_scheduler),
        persisted_authorized=real_authorized,execution_gate=real_gate,
        account_selected=bool(selected_real)
    )
    mode = configured_mode if configured_mode in ("demo", "real") else "unknown"
    selected_runtime = (real_runtime if mode == "real" else demo_runtime
                        if mode == "demo" else
                        {"running_now":None,"running_evidence":"UNKNOWN_MODE",
                         "running_state":"UNKNOWN","state_stale":True,
                         "scope":"LOCAL_PROCESS_PLUS_DATABASE"})
    account = selected_real if mode == "real" else selected_demo if mode == "demo" else None
    contracts = contract_summary(
        session,user.id,account_mode=mode,account_id=account
    )
    return {
        "contract_version": 2,
        "configured_mode": mode,
        "configured": configured,
        "selected_account": account,
        "session_valid": True,  # The caller already authenticated the session.
        "real_capability": {
            "configured": configured and mode == "real" and bool(selected_real),
            "technical_feature_enabled": bool(real_feature_gate),
            "administrative_release_approved": bool(real_release_approved),
            "technically_available": bool(real_feature_gate),
            "available_for_execution": bool(real_gate),
            "account_authorized": real_authorized,
            "running_now": real_runtime["running_now"],
            "running_evidence": real_runtime["running_evidence"],
        },
        "demo_capability": {
            "configured": configured and mode == "demo",
            "available_for_execution": bool(demo_gate),
            "resume_authorized": demo_authorized,
            "running_now": demo_runtime["running_now"],
            "running_evidence": demo_runtime["running_evidence"],
        },
        "running_now": selected_runtime["running_now"],
        "running_evidence": selected_runtime["running_evidence"],
        "running_state": selected_runtime["running_state"],
        "state_stale": selected_runtime["state_stale"],
        "state_scope": selected_runtime["scope"],
        "contracts": contracts,
        "reconciliation_pending": contracts.get("requires_reconciliation")
                                  if contracts["known"] else None,
        "observed_at": db.utcnow().astimezone(timezone.utc).isoformat(),
    }

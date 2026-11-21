"""Small per-user NEXORA 2.0 research-visit journal.

Reuses the existing historical_research_reports table. Only server-generated
visit timestamps and enum labels are stored: no trades, performance estimates,
market data, account identifiers, broker tokens, or user-supplied reports.

The general research-retention task applies to these entries too: 10 entries
maximum per user, older entries may disappear after 7 days, newest 3 remain.
This is study history, never broker history or validated execution evidence.
"""
import json
from datetime import timedelta, timezone

from sqlalchemy import delete, select

from . import db
from .research_persistence import HistoricalResearchReport

KIND_LABELS = {
    "intelligence": "Consulta Intelligence",
    "strategies": "Comparação histórica de estratégias",
}
HISTORY_LIMIT = 10
MIN_REPEAT_SECONDS = 5


def personal_research_key(user_id):
    """Derive the tenant namespace exclusively from the authenticated user."""
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id < 1:
        raise ValueError("Identificador de usuário inválido")
    return f"NEXORA2_USER_{user_id}_RESEARCH_VISIT_V1"


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _as_public_visit(row):
    try:
        data = json.loads(row.payload_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("kind") not in KIND_LABELS:
        return None
    if data.get("source") != "NEXORA2_USER_STUDY_VISIT":
        return None
    return {
        "id": row.id,
        "kind": data["kind"],
        "label": KIND_LABELS[data["kind"]],
        "at": _utc(row.created_at).isoformat(),
        "scope": "OWN_RESEARCH_VISITS",
    }


def list_personal_visits(session, user_id):
    key = personal_research_key(user_id)
    rows = session.scalars(
        select(HistoricalResearchReport)
        .where(HistoricalResearchReport.research_key == key)
        .order_by(HistoricalResearchReport.created_at.desc(),
                  HistoricalResearchReport.id.desc())
        .limit(HISTORY_LIMIT)
    ).all()
    return [item for row in rows if (item := _as_public_visit(row)) is not None]


def record_personal_visit(session, user_id, kind):
    if kind not in KIND_LABELS:
        raise ValueError("Tipo de pesquisa desconhecido")
    key = personal_research_key(user_id)
    now = db.utcnow()
    last = session.scalar(
        select(HistoricalResearchReport)
        .where(HistoricalResearchReport.research_key == key)
        .order_by(HistoricalResearchReport.created_at.desc(),
                  HistoricalResearchReport.id.desc())
        .limit(1)
    )
    previous = _as_public_visit(last) if last else None
    if (previous and previous["kind"] == kind and
            now - _utc(last.created_at) < timedelta(seconds=MIN_REPEAT_SECONDS)):
        # Ignore a rapid repeat of the SAME study; keep distinct study modes.
        return {"recorded": False, "reason": "RECENT_VISIT", "item": previous}

    record = HistoricalResearchReport(
        research_key=key,
        created_at=now,
        period_from=now,
        period_to=now,
        payload_json=json.dumps({
            "kind": kind,
            "source": "NEXORA2_USER_STUDY_VISIT",
            "orders_sent": 0,
        }, separators=(",", ":"), allow_nan=False),
    )
    session.add(record)
    session.flush()
    older_ids = session.scalars(
        select(HistoricalResearchReport.id)
        .where(HistoricalResearchReport.research_key == key)
        .order_by(HistoricalResearchReport.created_at.desc(),
                  HistoricalResearchReport.id.desc())
        .offset(HISTORY_LIMIT)
    ).all()
    if older_ids:
        session.execute(
            delete(HistoricalResearchReport)
            .where(HistoricalResearchReport.research_key == key,
                   HistoricalResearchReport.id.in_(older_ids))
        )
    item = _as_public_visit(record)
    session.commit()
    return {"recorded": True, "item": item}

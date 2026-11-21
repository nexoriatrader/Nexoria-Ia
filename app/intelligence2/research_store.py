"""Tenant-scoped research evidence in the existing historical reports table.

No migration, new table, shared cache, broker interface or order effects.
All reads/writes use the authenticated user_id, never a user-supplied ID.
"""
import hashlib
import hmac
import json
from datetime import datetime, timezone

from sqlalchemy import delete, select

from ..research_persistence import HistoricalResearchReport
from .historical_research import VERSION, summary

MAX_PER_USER = 5


def _key(user_id: int) -> str:
    if type(user_id) is not int or user_id < 1:
        raise ValueError("Identificador de usuário inválido")
    return f"NEXORA2_U{user_id}_OHLC_V1"


def persist(session, user_id: int, report: dict) -> int:
    key = _key(user_id)
    data = json.dumps(report, allow_nan=False, separators=(",", ":"),
                      sort_keys=True)
    start = datetime.fromtimestamp(report["period_from_epoch"], timezone.utc)
    end = datetime.fromtimestamp(report["period_to_epoch"], timezone.utc)
    record = HistoricalResearchReport(
        research_key=key, created_at=datetime.now(timezone.utc),
        period_from=start, period_to=end, payload_json=data,
    )
    session.add(record)
    session.flush()
    older = session.scalars(
        select(HistoricalResearchReport.id)
        .where(HistoricalResearchReport.research_key == key)
        .order_by(HistoricalResearchReport.created_at.desc(),
                  HistoricalResearchReport.id.desc())
        .offset(MAX_PER_USER)
    ).all()
    if older:
        session.execute(delete(HistoricalResearchReport).where(
            HistoricalResearchReport.research_key == key,
            HistoricalResearchReport.id.in_(older),
        ))
    session.commit()
    return record.id


def list_own(session, user_id: int) -> list[dict]:
    rows = session.execute(
        select(HistoricalResearchReport.id,
               HistoricalResearchReport.created_at,
               HistoricalResearchReport.period_from,
               HistoricalResearchReport.period_to)
        .where(HistoricalResearchReport.research_key == _key(user_id))
        .order_by(HistoricalResearchReport.created_at.desc(),
                  HistoricalResearchReport.id.desc())
        .limit(MAX_PER_USER)
    ).all()
    return [{
        "id": row.id,
        "created_at": row.created_at.isoformat(),
        "period_from": row.period_from.isoformat(),
        "period_to": row.period_to.isoformat(),
        "source": "USER_SUPPLIED_UNVERIFIED_OHLC",
    } for row in rows]


def get_own(session, user_id: int, report_id: int) -> dict | None:
    if type(report_id) is not int or report_id < 1:
        return None
    row = session.scalar(select(HistoricalResearchReport).where(
        HistoricalResearchReport.id == report_id,
        HistoricalResearchReport.research_key == _key(user_id),
    ))
    if row is None:
        return None
    if len(row.payload_json) > 400_000:
        raise ValueError("Relatório armazenado excedeu o limite de leitura")
    try:
        report = json.loads(row.payload_json)
    except (TypeError, ValueError):
        raise ValueError("Relatório armazenado inválido") from None
    if not isinstance(report, dict) or report.get("version") != VERSION:
        raise ValueError("Versão de relatório não compatível")
    if (report.get("source") != "USER_SUPPLIED_UNVERIFIED_OHLC"
            or report.get("execution_allowed") is not False
            or report.get("orders_sent") != 0):
        raise ValueError("Metadados da pesquisa salva inconsistentes")
    original = report.get("input")
    if not isinstance(original, dict):
        raise ValueError("Entrada original ausente da pesquisa salva")
    evidence = report.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("Evidência de pesquisa ausente")
    claimed = evidence.get("input_sha256")
    if not isinstance(claimed, str) or len(claimed) != 64:
        raise ValueError("Hash da evidência não reconhecido")
    try:
        canonical = json.dumps(original, sort_keys=True, separators=(",", ":"),
                               allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Entrada original não é verificável") from None
    actual = hashlib.sha256(canonical).hexdigest()
    if not hmac.compare_digest(actual, claimed):
        raise ValueError("Integridade da evidência alterada; pesquisa indisponível")
    return report


def details(report: dict) -> dict:
    return {"report": summary(report),
            "input": report["input"],
            "scope": "OWN_UNVERIFIED_HISTORICAL_RESEARCH"}

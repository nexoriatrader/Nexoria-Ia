"""I04 central public knowledge, evidence lineage, retrieval and decision log.

Storage: the existing db.SessionLocal, PostgreSQL in production, SQLite in
disposable tests. No personal/financial rows, cache, model training, signal
dispatch, broker purchase or automatic knowledge promotion.

Every write is explicit, revision-checked, and logged transactionally.
Only ACTIVE, current, integrity-verified knowledge is reusable.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .. import db

STATES = (
    "DISCOVERED", "UNDER_VALIDATION", "VALIDATED", "ACTIVE",
    "REVIEW_REQUIRED", "INVALIDATED", "REVALIDATED",
)
TRANSITIONS = {
    "DISCOVERED": {"UNDER_VALIDATION", "INVALIDATED"},
    "UNDER_VALIDATION": {"VALIDATED", "INVALIDATED"},
    "VALIDATED": {"ACTIVE", "INVALIDATED"},
    "ACTIVE": {"REVIEW_REQUIRED", "INVALIDATED"},
    "REVIEW_REQUIRED": {"INVALIDATED", "REVALIDATED"},
    "INVALIDATED": {"REVALIDATED"},
    "REVALIDATED": {"ACTIVE", "INVALIDATED"},
}
CATEGORIES = frozenset(("MARKET", "STRATEGY", "SIGNAL", "MODEL", "EXECUTION"))
SOURCES = frozenset(("PUBLIC_CONTEXT", "PUBLIC_CATALOG", "RESEARCH_REPORT",
                    "MODEL_ARTIFACT", "AGGREGATED_EXECUTION"))
SAFE_PAYLOAD = {
    "regime", "direction", "data_quality", "strategy_family", "strategy_id",
    "signal_pattern", "model_id", "model_version", "i03_context_version",
    "i03_timeframe_version", "source_version", "timeframe_seconds",
    "confidence_heuristic", "realized_volatility", "trend_efficiency",
    "sample_size", "holdout_samples", "aggregate_size", "latency_ms_p50",
    "latency_ms_p95", "error_rate", "success_rate", "execution_quality_bucket",
}
NONNEGATIVE = {"timeframe_seconds", "sample_size", "holdout_samples",
               "aggregate_size", "latency_ms_p50", "latency_ms_p95",
               "realized_volatility"}
RATES = {"error_rate", "success_rate", "confidence_heuristic",
         "trend_efficiency"}
_TEXT = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")
_SYMBOL = re.compile(r"[A-Za-z0-9_]{2,30}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
REVISION = "i04_knowledge_base_v1"
VERSION = "intelligence2_knowledge_base_v1"
MIN_RESEARCH_OBSERVATIONS = 72
MIN_HOLDOUT_OBSERVATIONS = 100
MIN_AGGREGATE_GROUP = 30


class KnowledgeError(ValueError):
    pass


class KnowledgeConflict(KnowledgeError):
    pass


class KnowledgeUnavailable(KnowledgeError):
    pass


def enabled() -> bool:
    return os.getenv("NEXORA_INTELLIGENCE_KNOWLEDGE_ENABLED", "false").lower() == "true"


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise KnowledgeError("Horário de conhecimento inválido")
    if value.tzinfo is None:
        # SQLite can drop timezone info; storage is always UTC by contract.
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat()


def _reason(code: str) -> str:
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,99}", code):
        raise KnowledgeError("Justificativa deve ser um código público auditável")
    return code


def _token(value: str, *, field: str, max_len: int = 100) -> str:
    if not isinstance(value, str) or len(value) > max_len or not _TEXT.fullmatch(value):
        raise KnowledgeError("Identificador público inválido: " + field)
    return value


def _payload(data: dict) -> tuple[str, str]:
    if not isinstance(data, dict) or not data or len(data) > 20:
        raise KnowledgeError("Payload público limitado a atributos estruturados")
    if not set(data) <= SAFE_PAYLOAD:
        raise KnowledgeError("Payload contém atributo não permitido")
    normalized = {}
    for key, value in data.items():
        if isinstance(value, bool) or value is None or isinstance(value, (list, dict)):
            raise KnowledgeError("Atributo do conhecimento não é escalar público")
        if isinstance(value, str):
            normalized[key] = _token(value, field=key)
        elif isinstance(value, (int, float)):
            if not math.isfinite(value) or abs(value) > 1_000_000_000:
                raise KnowledgeError("Valor numérico inválido")
            if key in NONNEGATIVE and value < 0:
                raise KnowledgeError("Métrica pública negativa")
            if key in RATES and not 0 <= value <= 1:
                raise KnowledgeError("Taxa fora do intervalo")
            normalized[key] = value
        else:
            raise KnowledgeError("Tipo de atributo não autorizado")
    if "aggregate_size" in normalized and normalized["aggregate_size"] < MIN_AGGREGATE_GROUP:
        raise KnowledgeError("Agregação insuficiente para anonimato")
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return canonical, hashlib.sha256(canonical.encode()).hexdigest()


def _identity(category: str, market: str, symbol: str, timeframe_seconds: int,
              subject: str, model_version: str) -> tuple[str, tuple]:
    if category not in CATEGORIES:
        raise KnowledgeError("Categoria não suportada")
    market = _token(market, field="market", max_len=64)
    if symbol != "" and (
        not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
    ):
        raise KnowledgeError("Símbolo público inválido")
    if type(timeframe_seconds) is not int or not 0 <= timeframe_seconds <= 86_400:
        raise KnowledgeError("Timeframe de conhecimento inválido")
    subject = _token(subject, field="subject")
    model_version = _token(model_version, field="model_version", max_len=64)
    components = (category, market, symbol, timeframe_seconds, subject, model_version)
    token = hashlib.sha256(json.dumps(components, separators=(",", ":")).encode()).hexdigest()
    return token, components


def _snapshot_digest(rows: list[db.IntelligenceKnowledgeEvidence]) -> str:
    values = [{
        "source_type": x.source_type, "source_ref": x.source_ref,
        "source_digest": x.source_digest, "origin_group": x.origin_group,
        "verified": x.verified, "observation_count": x.observation_count,
        "group_size": x.group_size,
        "period_from": _timestamp(x.period_from),
        "period_to": _timestamp(x.period_to),
    } for x in rows]
    values.sort(key=lambda x: (x["source_type"], x["source_ref"], x["source_digest"]))
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _evidence(session, key: int) -> list[db.IntelligenceKnowledgeEvidence]:
    return session.scalars(select(db.IntelligenceKnowledgeEvidence).where(
        db.IntelligenceKnowledgeEvidence.knowledge_id == key,
    ).order_by(db.IntelligenceKnowledgeEvidence.id)).all()


def _record(session, item, *, revision: int, from_state: str | None,
            to_state: str, decision: str, reason_code: str,
            evidence_digest: str, now: datetime):
    session.add(db.IntelligenceKnowledgeDecision(
        knowledge_id=item.id, revision=revision,
        from_state=from_state, to_state=to_state,
        decision=decision, reason_code=reason_code,
        evidence_digest=evidence_digest, recorded_at=now,
    ))


def _read_integrity(session, item) -> list:
    """Fail closed if public payload, evidence or last decision has changed."""
    try:
        payload = json.loads(item.payload_json)
        canonical, digest = _payload(payload)
    except (KnowledgeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise KnowledgeUnavailable("Payload de conhecimento corrompido") from exc
    if canonical != item.payload_json or digest != item.payload_digest:
        raise KnowledgeUnavailable("Assinatura do conhecimento divergente")
    rows = _evidence(session, item.id)
    log = session.scalars(select(db.IntelligenceKnowledgeDecision).where(
        db.IntelligenceKnowledgeDecision.knowledge_id == item.id,
    ).order_by(db.IntelligenceKnowledgeDecision.revision)).all()
    if (len(log) != item.revision
        or [event.revision for event in log] != list(range(1, item.revision + 1))
        or log[-1].to_state != item.state
        or log[-1].evidence_digest != _snapshot_digest(rows)):
        raise KnowledgeUnavailable("Cadeia de decisões ou evidências incompatível")
    return rows


def _public_record(item, *, now: datetime, observed_regime: str | None = None) -> dict:
    expired = not (_utc(item.valid_from) <= now < _utc(item.valid_until))
    overdue = now >= _utc(item.next_review_at)
    regime_mismatch = observed_regime is not None and (
        item.regime not in ("UNKNOWN", observed_regime)
    )
    allowed = item.state == "ACTIVE" and not (
        expired or overdue or regime_mismatch
    )
    reason = (
        "INACTIVE_STATE" if item.state != "ACTIVE" else
        "VALIDITY_EXPIRED" if expired else
        "PERIODIC_REVIEW_DUE" if overdue else
        "REGIME_CHANGED" if regime_mismatch else "CURRENT_VERIFIED_ACTIVE"
    )
    return {
        "id": item.id, "version": VERSION, "category": item.category,
        "market": item.market, "symbol": item.symbol,
        "timeframe_seconds": item.timeframe_seconds, "subject": item.subject,
        "model_version": item.model_version, "regime": item.regime,
        "state": item.state, "revision": item.revision,
        "payload": json.loads(item.payload_json),
        "payload_sha256": item.payload_digest,
        "valid_from": _timestamp(item.valid_from),
        "valid_until": _timestamp(item.valid_until),
        "next_review_at": _timestamp(item.next_review_at),
        "reason_code": item.reason_code,
        "reuse_status": reason, "reusable_for_research": allowed,
        "approved_for_trading": False, "execution_allowed": False,
    }


def discover(*, category: str, market: str, symbol: str, subject: str,
             model_version: str, timeframe_seconds: int, regime: str,
             attributes: dict, valid_from: datetime, valid_until: datetime,
             next_review_at: datetime, now: datetime | None = None) -> dict:
    """Idempotent public knowledge DISCOVERED only; never ACTIVE by discovery."""
    now = _utc(now or db.utcnow())
    from_at, until, review = map(_utc, (valid_from, valid_until, next_review_at))
    if not (from_at <= now < until and now < review <= until):
        raise KnowledgeError("Período/revisão inválido ou vencido")
    key, components = _identity(category, market, symbol, timeframe_seconds,
                                subject, model_version)
    regime = _token(regime, field="regime", max_len=40)
    canonical, digest = _payload(attributes)
    if category == "EXECUTION" and attributes.get("aggregate_size", 0) < MIN_AGGREGATE_GROUP:
        raise KnowledgeError("Execução apenas em agregados minimizados")
    with db.SessionLocal() as session:
        try:
            prior = session.scalar(select(db.IntelligenceKnowledge).where(
                db.IntelligenceKnowledge.identity_key == key,
            ).with_for_update())
            if prior is not None:
                if prior.payload_digest != digest or prior.regime != regime:
                    raise KnowledgeConflict("Identidade já existe com outro conteúdo/regime")
                _read_integrity(session, prior)
                return _public_record(prior, now=now)
            item = db.IntelligenceKnowledge(
                identity_key=key, category=category, market=market,
                symbol=symbol, timeframe_seconds=timeframe_seconds,
                subject=subject, model_version=model_version, regime=regime,
                state="DISCOVERED", revision=1, payload_json=canonical,
                payload_digest=digest, valid_from=from_at, valid_until=until,
                next_review_at=review, reason_code="NEW_PUBLIC_DISCOVERY",
                created_at=now, updated_at=now,
            )
            session.add(item)
            session.flush()
            _record(session, item, revision=1, from_state=None,
                    to_state="DISCOVERED", decision="DISCOVERY",
                    reason_code="NEW_PUBLIC_DISCOVERY",
                    evidence_digest=_snapshot_digest([]), now=now)
            session.commit()
            return _public_record(item, now=now)
        except IntegrityError as exc:
            session.rollback()
            existing = session.scalar(select(db.IntelligenceKnowledge).where(
                db.IntelligenceKnowledge.identity_key == key))
            if existing is not None and existing.payload_digest == digest and existing.regime == regime:
                _read_integrity(session, existing)
                return _public_record(existing, now=now)
            raise KnowledgeConflict("Identidade de conhecimento concorrente") from exc


def _locked(session, knowledge_id: int, expected_revision: int):
    if type(knowledge_id) is not int or knowledge_id <= 0:
        raise KnowledgeError("Conhecimento inválido")
    if type(expected_revision) is not int or expected_revision < 1:
        raise KnowledgeError("Revisão esperada obrigatória")
    item = session.get(db.IntelligenceKnowledge, knowledge_id, with_for_update=True)
    if item is None:
        raise KnowledgeUnavailable("Conhecimento não encontrado")
    if item.revision != expected_revision:
        raise KnowledgeConflict("Revisão obsoleta: CAS não autorizado")
    _read_integrity(session, item)
    return item


def _cas(session, item, expected_revision: int, *, state: str, reason: str,
         decision: str, now: datetime, digest: str):
    result = session.execute(update(db.IntelligenceKnowledge).where(
        db.IntelligenceKnowledge.id == item.id,
        db.IntelligenceKnowledge.revision == expected_revision,
        db.IntelligenceKnowledge.state == item.state,
    ).values(
        state=state, revision=expected_revision+1,
        reason_code=reason, updated_at=now,
    ))
    if result.rowcount != 1:
        raise KnowledgeConflict("Atualização concorrente rejeitada")
    _record(session, item, revision=expected_revision+1,
            from_state=item.state, to_state=state, decision=decision,
            reason_code=reason, evidence_digest=digest, now=now)


def append_evidence(*, knowledge_id: int, expected_revision: int,
                    source_type: str, source_ref: str, source_digest: str,
                    origin_group: str, observation_count: int,
                    period_from: datetime, period_to: datetime,
                    verified: bool, group_size: int = 0,
                    now: datetime | None = None) -> dict:
    """Append metadata-only public evidence and its immutable decision event.

    Independent sources require distinct origin_group; group_size >=30
    and observation_count >=30 for aggregated execution evidence.
    """
    now = _utc(now or db.utcnow())
    if source_type not in SOURCES:
        raise KnowledgeError("Origem de evidência não permitida")
    if not all(isinstance(x, str) and _DIGEST.fullmatch(x) for x in
               (source_ref, source_digest)):
        raise KnowledgeError("Origem deve ser digest público SHA256")
    origin_group = _token(origin_group, field="origin_group", max_len=64)
    if type(verified) is not bool:
        raise KnowledgeError("Validação de evidência inválida")
    if type(observation_count) is not int or observation_count < 1:
        raise KnowledgeError("Amostra de evidência inválida")
    if type(group_size) is not int or group_size < 0:
        raise KnowledgeError("Tamanho de agregado inválido")
    start, end = map(_utc, (period_from, period_to))
    if start > end or end > now:
        raise KnowledgeError("Evidência futura ou período inválido")
    if source_type == "AGGREGATED_EXECUTION" and (
        group_size < MIN_AGGREGATE_GROUP or observation_count < MIN_AGGREGATE_GROUP
    ):
        raise KnowledgeError("Fonte financeira sem agregado mínimo")
    if source_type != "AGGREGATED_EXECUTION" and group_size != 0:
        raise KnowledgeError("Grupo financeiro em origem não financeira")
    with db.SessionLocal() as session:
        item = _locked(session, knowledge_id, expected_revision)
        if item.state in ("ACTIVE", "VALIDATED", "REVALIDATED"):
            raise KnowledgeError("Revisão deve preceder a adição de evidências")
        current = _evidence(session, knowledge_id)
        if any(x.source_digest == source_digest for x in current):
            # Idempotent retry must not create a second revision.
            return _public_record(item, now=now)
        if any(x.source_ref == source_ref for x in current):
            raise KnowledgeConflict("Mesma origem alterada; criar nova versão do conhecimento")
        row = db.IntelligenceKnowledgeEvidence(
            knowledge_id=knowledge_id, source_type=source_type,
            source_ref=source_ref, source_digest=source_digest,
            origin_group=origin_group, observation_count=observation_count,
            group_size=group_size, period_from=start, period_to=end,
            verified=verified, recorded_at=now,
        )
        session.add(row)
        session.flush()
        digest = _snapshot_digest(current + [row])
        _cas(session, item, expected_revision, state=item.state,
             reason="EVIDENCE_APPENDED", decision="EVIDENCE_APPENDED",
             now=now, digest=digest)
        session.commit()
        session.refresh(item)
        return _public_record(item, now=now)


def _enough_evidence(item, rows, *, now: datetime, after: datetime | None = None) -> bool:
    eligible = [
        row for row in rows
        if row.verified is True and row.observation_count >= MIN_RESEARCH_OBSERVATIONS
        and _utc(row.period_to) <= now
        and (after is None or _utc(row.recorded_at) > after)
    ]
    if len(eligible) < 2 or len({r.origin_group for r in eligible}) < 2:
        return False
    if item.category == "EXECUTION":
        return (int(json.loads(item.payload_json).get("aggregate_size", 0)) >= MIN_AGGREGATE_GROUP
                and len([e for e in eligible if e.source_type == "AGGREGATED_EXECUTION"
                         and e.group_size >= MIN_AGGREGATE_GROUP]) >= 2)
    if item.category == "STRATEGY":
        return any(e.source_type == "RESEARCH_REPORT"
                   and e.observation_count >= MIN_HOLDOUT_OBSERVATIONS
                   for e in eligible)
    if item.category == "MODEL":
        return False  # No training, model approval or promotion in I04.
    return True


def change_state(*, knowledge_id: int, expected_revision: int,
                 target_state: str, reason_code: str,
                 now: datetime | None = None) -> dict:
    """Explicit workflow only; never promotes on scanner/clock observation."""
    now = _utc(now or db.utcnow())
    reason_code = _reason(reason_code)
    if target_state not in STATES:
        raise KnowledgeError("Estado de conhecimento desconhecido")
    with db.SessionLocal() as session:
        item = _locked(session, knowledge_id, expected_revision)
        if target_state not in TRANSITIONS[item.state]:
            raise KnowledgeError("Transição de conhecimento proibida")
        if item.category == "MODEL" and target_state in (
            "VALIDATED", "ACTIVE", "REVALIDATED"
        ):
            raise KnowledgeError("I04 não promove modelos de aprendizado")
        rows = _evidence(session, item.id)
        if target_state in ("VALIDATED", "ACTIVE", "REVALIDATED"):
            if not (_utc(item.valid_from) <= now < _utc(item.valid_until)
                    and now < _utc(item.next_review_at)):
                raise KnowledgeError("Conhecimento vencido não pode ser promovido")
            after = None
            if target_state == "REVALIDATED":
                prior = session.scalars(select(db.IntelligenceKnowledgeDecision).where(
                    db.IntelligenceKnowledgeDecision.knowledge_id == item.id,
                    db.IntelligenceKnowledgeDecision.to_state.in_((
                        "INVALIDATED", "REVIEW_REQUIRED",
                    )),
                ).order_by(db.IntelligenceKnowledgeDecision.revision.desc())).first()
                after = _utc(prior.recorded_at) if prior else now
            if not _enough_evidence(item, rows, now=now, after=after):
                raise KnowledgeError("Evidências independentes e verificadas insuficientes")
        digest = _snapshot_digest(rows)
        _cas(session, item, expected_revision, state=target_state,
             reason=reason_code, decision="STATE_TRANSITION", now=now,
             digest=digest)
        session.commit()
        session.refresh(item)
        return _public_record(item, now=now)


def get_knowledge(knowledge_id: int, *, now: datetime | None = None,
                  observed_regime: str | None = None,
                  include_lineage: bool = True) -> dict:
    now = _utc(now or db.utcnow())
    if observed_regime is not None:
        observed_regime = _token(observed_regime, field="observed_regime", max_len=40)
    with db.SessionLocal() as session:
        item = session.get(db.IntelligenceKnowledge, knowledge_id)
        if item is None:
            raise KnowledgeUnavailable("Conhecimento inexistente")
        rows = _read_integrity(session, item)
        public = _public_record(item, now=now, observed_regime=observed_regime)
        if include_lineage:
            public["evidence"] = [{
                "source_type": r.source_type, "source_ref_sha256": r.source_ref,
                "source_sha256": r.source_digest,
                "origin_group": r.origin_group,
                "observations": r.observation_count,
                "aggregation_group_size": r.group_size,
                "verified": r.verified, "period_from": _timestamp(r.period_from),
                "period_to": _timestamp(r.period_to),
            } for r in rows]
            decisions = session.scalars(select(db.IntelligenceKnowledgeDecision).where(
                db.IntelligenceKnowledgeDecision.knowledge_id == item.id
            ).order_by(db.IntelligenceKnowledgeDecision.revision)).all()
            public["decisions"] = [{
                "revision": d.revision, "from_state": d.from_state,
                "to_state": d.to_state, "decision": d.decision,
                "reason_code": d.reason_code,
                "evidence_sha256": d.evidence_digest,
                "recorded_at": _timestamp(d.recorded_at),
            } for d in decisions]
        return public


def retrieve_active(*, category: str, market: str, symbol: str = "",
                    observed_regime: str | None = None,
                    now: datetime | None = None, limit: int = 50) -> dict:
    """Only fresh ACTIVE records may be consumed by future research modules."""
    now = _utc(now or db.utcnow())
    if category not in CATEGORIES:
        raise KnowledgeError("Categoria inválida")
    market = _token(market, field="market", max_len=64)
    if symbol and not _SYMBOL.fullmatch(symbol):
        raise KnowledgeError("Símbolo inválido")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise KnowledgeError("Limite inválido")
    if observed_regime is not None:
        observed_regime = _token(observed_regime, field="observed_regime", max_len=40)
    with db.SessionLocal() as session:
        items = session.scalars(select(db.IntelligenceKnowledge).where(
            db.IntelligenceKnowledge.category == category,
            db.IntelligenceKnowledge.market == market,
            db.IntelligenceKnowledge.symbol == symbol,
        ).order_by(db.IntelligenceKnowledge.id).limit(limit)).all()
        accepted, rejected = [], []
        for item in items:
            # Even inactive rows are integrity-checked, not silently skipped.
            _read_integrity(session, item)
            view = _public_record(item, now=now, observed_regime=observed_regime)
            if view["reusable_for_research"]:
                accepted.append(view)
            else:
                rejected.append({
                    "id": item.id, "state": item.state,
                    "reason": view["reuse_status"],
                })
        return {
            "version": VERSION, "category": category, "market": market,
            "symbol": symbol, "observed_regime": observed_regime,
            "active": accepted, "excluded": rejected,
            "read_only": True, "execution_allowed": False,
        }


def review_due_knowledge(*, now: datetime | None = None, limit: int = 100) -> int:
    """Safe-only periodic downgrade; call from existing research loop if enabled.

    Fails closed if table/migration unavailable. No promotions, deletions,
    broker calls, parallel timers or user-specific evidence.
    """
    if not enabled():
        return 0
    now = _utc(now or db.utcnow())
    if type(limit) is not int or not 1 <= limit <= 100:
        raise KnowledgeError("Limite de revisão inválido")
    with db.SessionLocal() as session:
        ids = session.scalars(select(db.IntelligenceKnowledge.id).where(
            db.IntelligenceKnowledge.state == "ACTIVE",
            (db.IntelligenceKnowledge.next_review_at <= now)
            | (db.IntelligenceKnowledge.valid_until <= now),
        ).order_by(db.IntelligenceKnowledge.id).limit(limit)).all()
    changed = 0
    for knowledge_id in ids:
        try:
            with db.SessionLocal() as session:
                row = session.get(db.IntelligenceKnowledge, knowledge_id)
                revision = row.revision if row else None
            if revision is not None:
                change_state(knowledge_id=knowledge_id, expected_revision=revision,
                             target_state="REVIEW_REQUIRED",
                             reason_code="PERIODIC_REVIEW_DUE", now=now)
                changed += 1
        except KnowledgeConflict:
            continue  # Another worker safely handled the same knowledge.
    return changed


def invalidate_on_market_change(*, market: str, symbol: str,
                                observed_regime: str,
                                now: datetime | None = None) -> int:
    """Explicit, public-market-only downgrade. No user data or orders."""
    now = _utc(now or db.utcnow())
    market = _token(market, field="market", max_len=64)
    if not _SYMBOL.fullmatch(symbol):
        raise KnowledgeError("Símbolo público inválido")
    observed_regime = _token(observed_regime, field="observed_regime", max_len=40)
    with db.SessionLocal() as session:
        rows = session.scalars(select(db.IntelligenceKnowledge).where(
            db.IntelligenceKnowledge.market == market,
            db.IntelligenceKnowledge.symbol == symbol,
            db.IntelligenceKnowledge.state.in_(("ACTIVE", "REVIEW_REQUIRED")),
            db.IntelligenceKnowledge.regime.not_in(("UNKNOWN", observed_regime)),
        ).order_by(db.IntelligenceKnowledge.id).limit(100)).all()
        candidates = [(row.id, row.revision) for row in rows]
    changed = 0
    for knowledge_id, revision in candidates:
        try:
            change_state(knowledge_id=knowledge_id, expected_revision=revision,
                         target_state="INVALIDATED",
                         reason_code="PUBLIC_MARKET_REGIME_CHANGED", now=now)
            changed += 1
        except KnowledgeConflict:
            continue
    return changed

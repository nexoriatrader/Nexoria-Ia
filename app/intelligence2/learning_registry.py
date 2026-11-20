"""I05 research-only, auditable model governance backed by existing I04 DB.

No new table/cache or broker client. Candidates and append-only releases use
HistoricalResearchReport; validated model METADATA uses I04 MODEL knowledge
and its existing CAS/evidence/decision log. No financial strategy is updated.

Promotion is opt-in, explicit, gated, and research-only; automatic rollback
changes only the public research release pointer, never a broker account.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, or_, text

from .. import db
from ..research_persistence import HistoricalResearchReport
from . import knowledge_base as kb
from .learning_engine import (
    VERSION, SOURCE_VERSION, EMBARGO, MIN_HOLDOUT, REGIMES,
    LearningError, _sha, _canonical, _valid_history, _predict,
    _wilson_lower, evaluate_public_regimes, learning_enabled, promotion_enabled,
)

ARTIFACT_PREFIX = "I05_ART_"
RELEASE_PREFIX = "I05_REL_"
MIN_MONITOR_SAMPLES = 200
BAD_WINDOWS_TO_ROLLBACK = 2


def _artifact_key(model_digest: str) -> str:
    if not isinstance(model_digest, str) or not kb._DIGEST.fullmatch(model_digest):
        raise LearningError("SHA público do modelo inválido")
    return ARTIFACT_PREFIX + model_digest[:48]


def _scope(market: str, symbol: str, timeframe_seconds: int) -> tuple:
    try:
        _, values=kb._identity("MARKET",market,symbol,timeframe_seconds,
                              "public_regime_model","i05_v1")
    except kb.KnowledgeError as exc:
        raise LearningError("Escopo público do aprendizado inválido") from exc
    return values[1:4]


def _release_key(scope: tuple) -> str:
    return RELEASE_PREFIX + _sha(scope)[:48]


def _reg_lock(session, key: str) -> None:
    """Transaction-scoped cross-process release lock; no new table/worker."""
    dialect=session.get_bind().dialect.name
    if dialect=="postgresql":
        lock_value=int.from_bytes(hashlib.sha256(key.encode()).digest()[:8],"big",signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"),{"k":lock_value})
    elif dialect=="sqlite":
        # Explicit writer lock on disposable/local SQLite; no cross-user data.
        session.execute(text("BEGIN IMMEDIATE"))
    else:
        raise LearningError("Banco sem locking de release validado")


def _artifact_row(session, model_digest: str):
    key=_artifact_key(model_digest)
    rows=session.scalars(select(HistoricalResearchReport).where(
        HistoricalResearchReport.research_key==key,
    ).order_by(HistoricalResearchReport.id.desc()).limit(2)).all()
    if len(rows)>1:
        raise kb.KnowledgeUnavailable("Artefato público duplicado")
    return rows[0] if rows else None


def _verify_artifact(row: HistoricalResearchReport) -> tuple[dict,dict]:
    if row is None:
        raise kb.KnowledgeUnavailable("Artefato de pesquisa inexistente")
    try:
        payload=json.loads(row.payload_json)
        if set(payload)!={"version","report","history","report_sha256","execution_allowed"}:
            raise ValueError("unknown fields")
        if payload["version"]!=VERSION or payload["execution_allowed"] is not False:
            raise ValueError("untrusted research release")
        rederived=evaluate_public_regimes(payload["history"])
        if (rederived!=payload["report"]
            or _sha(payload["report"])!=payload["report_sha256"]
            or _artifact_key(rederived["model_id_sha256"])!=row.research_key):
            raise ValueError("unverifiable OOS research")
        start=datetime.fromtimestamp(rederived["period_from_epoch"],tz=timezone.utc)
        end=datetime.fromtimestamp(rederived["period_to_epoch"],tz=timezone.utc)
        if (kb._utc(row.period_from)!=start or kb._utc(row.period_to)!=end):
            raise ValueError("research epoch mismatch")
    except (ValueError,TypeError,KeyError,IndexError,ZeroDivisionError) as exc:
        raise kb.KnowledgeUnavailable("Artefato/holdout incompatível") from exc
    return rederived,payload["history"]


def _model_match(item, report: dict):
    if not isinstance(item,db.IntelligenceKnowledge) or item.category!="MODEL":
        raise LearningError("Knowledge Base exige MODEL")
    if (item.market!=report["market"] or item.symbol!=report["symbol"]
        or item.timeframe_seconds!=report["timeframe_seconds"]
        or item.model_version!=report["model_version"]
        or json.loads(item.payload_json).get("model_id")!=report["model_id_sha256"]):
        raise kb.KnowledgeUnavailable("Modelo I04 não corresponde ao artefato")


def _release_event(session, scope: tuple, *, champion_id: int | None,
                   fallback_id: int | None, previous, reason: str,
                   event: str, now: datetime,
                   last_check_epoch: int | None = None,
                   bad_streak: int = 0) -> dict:
    if event not in ("PROMOTED", "MONITORED", "AUTO_ROLLBACK"):
        raise LearningError("Evento de release inválido")
    if previous is not None and kb._utc(previous[0].created_at)>kb._utc(now):
        raise LearningError("Release de pesquisa retroativo")
    entry={
        "version":VERSION,"scope":{
            "market":scope[0],"symbol":scope[1],
            "timeframe_seconds":scope[2],
        },
        "generation":(previous[1]["generation"]+1 if previous else 1),
        "event":event,"reason_code":kb._reason(reason),
        "champion_knowledge_id":champion_id,
        "fallback_knowledge_id":fallback_id,
        "previous_release_id":previous[0].id if previous else None,
        "previous_sha256":previous[1]["entry_sha256"] if previous else None,
        "bad_streak":bad_streak,
        "last_check_epoch":last_check_epoch,
        "recorded_at":kb._utc(now).isoformat(),
        "execution_allowed":False,"approved_for_trading":False,
    }
    entry["entry_sha256"]=_sha(entry)
    saved=HistoricalResearchReport(
        research_key=_release_key(scope),created_at=now,
        period_from=now,period_to=now,payload_json=_canonical(entry),
    )
    session.add(saved)
    session.flush()
    return {"id":saved.id,**entry}


def _release_history(session, scope: tuple):
    events=session.scalars(select(HistoricalResearchReport).where(
        HistoricalResearchReport.research_key==_release_key(scope),
    ).order_by(HistoricalResearchReport.id.asc())).all()
    previous=None
    for row in events:
        try:
            entry=json.loads(row.payload_json)
            digest=entry.pop("entry_sha256")
            if _sha(entry)!=digest:
                raise ValueError("release digest")
            entry["entry_sha256"]=digest
            expected_previous_id=previous[0].id if previous else None
            expected_previous_sha=previous[1]["entry_sha256"] if previous else None
            if (entry.get("version")!=VERSION
                or entry.get("scope")!={"market":scope[0],"symbol":scope[1],
                                      "timeframe_seconds":scope[2]}
                or entry.get("previous_release_id")!=expected_previous_id
                or entry.get("previous_sha256")!=expected_previous_sha
                or entry.get("generation")!=(previous[1]["generation"]+1 if previous else 1)
                or entry.get("execution_allowed") is not False
                or entry.get("approved_for_trading") is not False
                or kb._utc(row.created_at)!=kb._utc(
                    datetime.fromisoformat(entry["recorded_at"]))
                or (previous is not None
                    and kb._utc(row.created_at)<kb._utc(previous[0].created_at))):
                raise ValueError("release history inconsistency")
            previous=(row,entry)
        except (TypeError,ValueError,KeyError,IndexError) as exc:
            raise kb.KnowledgeUnavailable("Trilha de release I05 corrompida") from exc
    return previous


def _model_research_ready(session, knowledge_id: int | None, *, now: datetime,
                          state: str = "ACTIVE") -> bool:
    if knowledge_id is None:
        return False
    item=session.get(db.IntelligenceKnowledge,knowledge_id)
    if item is None or item.category!="MODEL" or item.state!=state:
        return False
    kb._read_integrity(session,item)
    view=kb._public_record(item,now=now)
    return (
        item.state==state
        and kb._utc(item.valid_from)<=now<kb._utc(item.valid_until)
        and now<kb._utc(item.next_review_at)
        and view["execution_allowed"] is False
        and view["reusable_for_research"] is True
    )


def _no_open_contracts(session):
    # Read-only global safety barrier; do not expose a tenant, identifier,
    # financial value or per-user state in release/knowledge.
    states=("PENDING","UNKNOWN","CONFIRMED")
    for model in (db.DemoOrder,db.RealOrder):
        open_id=session.scalar(select(model.id).where(or_(
            model.state.in_(states),model.active_slot.is_not(None),
        )).limit(1))
        if open_id is not None:
            raise LearningError("OPEN_OPERATIONS_BLOCK_RESEARCH_PROMOTION")


def _verified_model_evidence(session,item,report):
    evidence=kb._evidence(session,item.id)
    verified={e.origin_group:e for e in evidence
              if e.verified and e.source_type=="MODEL_ARTIFACT"}
    required={"PUBLIC_TRAIN_WINDOW","PUBLIC_HOLDOUT_WINDOW",
              "PUBLIC_INDEPENDENT_VALIDATION"}
    if (not required <= set(verified)
        or any(verified[k].observation_count < MIN_HOLDOUT for k in required)):
        raise LearningError("Duas validações temporais independentes exigidas")
    independent=verified["PUBLIC_INDEPENDENT_VALIDATION"]
    # A separate, later history with embargo cannot reuse the original
    # training or initial OOS holdout, even across process restarts.
    if kb._utc(independent.period_from).timestamp() <= (
        report["period_to_epoch"] + EMBARGO*report["timeframe_seconds"]
    ):
        raise LearningError("Segunda validação sobrepõe o holdout original")
    if (not report["research_eligible"]
        or report["holdout_samples"]<MIN_HOLDOUT
        or report["holdout_accuracy"]<report["baseline_accuracy"]+.04
        or report["wilson95_lower"]<report["baseline_accuracy"]+.01):
        raise LearningError("Challenger reprovado fora da amostra")
    summary=json.loads(independent.evidence_summary_json)
    if (summary.get("model_id")!=report["model_id_sha256"]
        or summary.get("source_version")!=SOURCE_VERSION
        or summary.get("sample_size")!=independent.observation_count
        or summary.get("success_rate",0)<summary.get("trend_efficiency",1)+.04
        or summary.get("confidence_heuristic",0)<summary.get("trend_efficiency",1)+.01):
        raise LearningError("Validação independente não demonstrou ganho robusto")
    return evidence


def stage_public_candidate(history: dict, *, market_knowledge_id: int,
                           now: datetime | None = None) -> dict:
    """Durably stage a public challenger in I04 with chronological evidence.

    Never validates or promotes automatically. Immutable research artifact
    is stored in the EXISTING HistoricalResearchReport table.
    """
    now=kb._utc(now or db.utcnow())
    report=evaluate_public_regimes(history)
    source=kb.get_knowledge(market_knowledge_id,now=now,
                            observed_regime=history["observations"][-1]["regime"])
    if (source["category"]!="MARKET" or not source["reusable_for_research"]
        or (source["market"],source["symbol"],source["timeframe_seconds"])
        !=(report["market"],report["symbol"],report["timeframe_seconds"])):
        raise LearningError("Fonte I04 MARKET inválida para o challenger")
    if not report["research_eligible"]:
        return {
            "state":"REJECTED_OOS", "report":report,
            "knowledge_id":None,"artifact_id":None,
            "execution_allowed":False,
        }
    stored={
        "version":VERSION,"report":report,"history":history,
        "report_sha256":_sha(report),"execution_allowed":False,
    }
    with db.SessionLocal() as session:
        _reg_lock(session,_artifact_key(report["model_id_sha256"]))
        artifact=_artifact_row(session,report["model_id_sha256"])
        if artifact is None:
            artifact=HistoricalResearchReport(
                research_key=_artifact_key(report["model_id_sha256"]),
                created_at=now,
                period_from=datetime.fromtimestamp(
                    report["period_from_epoch"],tz=timezone.utc),
                period_to=datetime.fromtimestamp(
                    report["period_to_epoch"],tz=timezone.utc),
                payload_json=_canonical(stored),
            )
            session.add(artifact)
            session.commit()
        else:
            existing,_=_verify_artifact(artifact)
            if existing!=report:
                raise kb.KnowledgeConflict("Modelo publicado com dados divergentes")
        artifact_id=artifact.id
    item=kb.discover(
        category="MODEL",market=report["market"],
        symbol=report["symbol"],timeframe_seconds=report["timeframe_seconds"],
        subject="public_regime_transition",model_version=report["model_version"],
        regime="UNKNOWN",
        attributes={
            "model_id":report["model_id_sha256"],
            "source_version":SOURCE_VERSION,
            "sample_size":report["train_samples"],
            "holdout_samples":report["holdout_samples"],
            "success_rate":report["holdout_accuracy"],
            "confidence_heuristic":report["wilson95_lower"],
        },
        valid_from=now-timedelta(minutes=1),
        valid_until=now+timedelta(days=30),
        next_review_at=now+timedelta(days=7),now=now)
    if item["state"]=="DISCOVERED":
        item=kb.change_state(
            knowledge_id=item["id"],expected_revision=item["revision"],
            target_state="UNDER_VALIDATION",reason_code="I05_OOS_EVIDENCE_PENDING",now=now)
    if item["state"]!="UNDER_VALIDATION":
        return {
            "state":item["state"],"knowledge_id":item["id"],
            "artifact_id":artifact_id,"report":report,"execution_allowed":False,
        }
    states,epochs=_valid_history(history)
    cut=int(len(states)*.64)
    hold_start=cut+EMBARGO
    pieces=[
        ("PUBLIC_TRAIN_WINDOW",states[:cut],epochs[0],epochs[cut-1]),
        ("PUBLIC_HOLDOUT_WINDOW",states[hold_start:],epochs[hold_start],epochs[-1]),
    ]
    for group,sequence,first,last in pieces:
        dig=_sha({"segment":group,"observations":sequence,
                  "start":first,"end":last})
        ref=_sha({"model_id":report["model_id_sha256"],
                  "research_report_id":artifact_id,"group":group})
        item=kb.append_evidence(
            knowledge_id=item["id"],expected_revision=item["revision"],
            source_type="MODEL_ARTIFACT",source_ref=ref,
            source_digest=dig,origin_group=group,
            observation_count=len(sequence),
            period_from=datetime.fromtimestamp(first,tz=timezone.utc),
            period_to=datetime.fromtimestamp(last,tz=timezone.utc),
            verified=True,summary={
                "model_id":report["model_id_sha256"],
                "source_version":SOURCE_VERSION,
                "sample_size":len(sequence),
            },now=now)
    return {"state":item["state"],"knowledge_id":item["id"],
            "artifact_id":artifact_id,"report":report,"execution_allowed":False}


def append_independent_public_validation(*, model_knowledge_id: int,
                                         model_digest: str,
                                         expected_revision: int,
                                         history: dict,
                                         now: datetime | None = None) -> dict:
    """Second, later verified public OOS window; strict embargo/no replays.

    Never retrains on the validation window. Uses the frozen model from the
    original I04 artifact and stores only minimized public evidence in KB.
    """
    now=kb._utc(now or db.utcnow())
    states,epochs=_valid_history(history)
    with db.SessionLocal() as session:
        model=kb._locked(session,model_knowledge_id,expected_revision)
        artifact=_artifact_row(session,model_digest)
        report,_=_verify_artifact(artifact)
        _model_match(model,report)
        if model.state!="UNDER_VALIDATION":
            raise LearningError("Modelo indisponível para validação posterior")
        if (history["market"],history["symbol"],history["timeframe_seconds"])!=(
            report["market"],report["symbol"],report["timeframe_seconds"]):
            raise LearningError("Segunda amostra não corresponde ao modelo")
        barrier=report["period_to_epoch"]+EMBARGO*report["timeframe_seconds"]
        indices=[i for i in range(1,len(epochs)) if epochs[i]>barrier]
        if len(indices)<MIN_HOLDOUT:
            raise LearningError("Nova amostra independente insuficiente")
        if int(now.timestamp())<epochs[-1]:
            raise LearningError("Dados da validação ainda não encerrados")
        matrix=report["artifact"]["transition_counts"]
        correct=sum(_predict(matrix,states[i-1])==states[i] for i in indices)
        baseline=sum(states[i-1]==states[i] for i in indices)
        n=len(indices)
        accuracy=correct/n
        base=baseline/n
        lower=_wilson_lower(correct,n)
        midpoint=n//2
        halves=(
            indices[:midpoint],indices[midpoint:]
        )
        half_lifts=[
            (sum(_predict(matrix,states[i-1])==states[i] for i in part)
             -sum(states[i-1]==states[i] for i in part))/len(part)
            for part in halves
        ]
        verified=bool(
            accuracy>=base+.04 and lower>=base+.01
            and all(lift>=.02 for lift in half_lifts)
        )
    # KB append opens an independent transaction with revision CAS; the
    # candidate cannot be promoted until this evidence is committed.
    result=kb.append_evidence(
        knowledge_id=model_knowledge_id,expected_revision=expected_revision,
        source_type="MODEL_ARTIFACT",
        source_ref=_sha({"model":model_digest,"window_start":epochs[indices[0]],
                         "window_end":epochs[indices[-1]],"kind":"INDEPENDENT"}),
        source_digest=_sha({
            "model":model_digest,
            "observations":[(epochs[i],states[i]) for i in indices],
            "kind":"PUBLIC_INDEPENDENT_VALIDATION",
        }),
        origin_group="PUBLIC_INDEPENDENT_VALIDATION",
        observation_count=n,
        period_from=datetime.fromtimestamp(epochs[indices[0]],tz=timezone.utc),
        period_to=datetime.fromtimestamp(epochs[indices[-1]],tz=timezone.utc),
        verified=verified,summary={
            "model_id":model_digest,"source_version":SOURCE_VERSION,
            "sample_size":n,"success_rate":round(accuracy,6),
            "trend_efficiency":round(base,6),
            "confidence_heuristic":round(lower,6),
        },now=now)
    return {
        "model_knowledge_id":model_knowledge_id,
        "revision":result["revision"],"verified":verified,
        "holdout_samples":n,"holdout_accuracy":round(accuracy,6),
        "baseline_accuracy":round(base,6),
        "reason_code":(
            "SECOND_PUBLIC_HOLDOUT_VERIFIED" if verified
            else "SECOND_PUBLIC_HOLDOUT_REJECTED"
        ),
        "execution_allowed":False,
    }


def validate_research_challenger(*, model_knowledge_id: int,
                                 model_digest: str,
                                 expected_revision: int,
                                 now: datetime | None = None) -> dict:
    """I05-only gated MODEL validation. I04 generic model promotion stays blocked."""
    if not learning_enabled():
        raise LearningError("I05 deve estar explicitamente habilitada")
    now=kb._utc(now or db.utcnow())
    with db.SessionLocal() as session:
        artifact=_artifact_row(session,model_digest)
        report,_=_verify_artifact(artifact)
        model=kb._locked(session,model_knowledge_id,expected_revision)
        _model_match(model,report)
        if model.state!="UNDER_VALIDATION":
            raise LearningError("Modelo não está aguardando validação I05")
        evidence=_verified_model_evidence(session,model,report)
        if (kb._utc(model.valid_from)>now
            or kb._utc(model.valid_until)<=now
            or kb._utc(model.next_review_at)<=now):
            raise LearningError("Modelo vencido exige novo treinamento")
        digest=kb._snapshot_digest(evidence)
        kb._cas(session,model,expected_revision,state="VALIDATED",
                reason="I05_OOS_REGIME_VALIDATED",
                decision="I05_RESEARCH_MODEL_VALIDATION",now=now,digest=digest)
        session.commit()
        session.refresh(model)
        return {
            "state":model.state,"revision":model.revision,
            "model_knowledge_id":model.id,
            "model_digest":model_digest,
            "execution_allowed":False,"approved_for_trading":False,
        }


def promote_research_champion(*, model_knowledge_id: int,
                              model_digest: str,
                              expected_model_revision: int,
                              expected_release_id: int | None,
                              approval_reason: str,
                              now: datetime | None = None) -> dict:
    """Controlled research-only promotion, never a financial release.

    REQUIRED: explicit deployment flag, complete OOS evidence, version CAS,
    no open operations anywhere, serialized Postgres advisory transaction.
    """
    if not promotion_enabled():
        raise LearningError("Promoção I05 desabilitada")
    reason=kb._reason(approval_reason)
    now=kb._utc(now or db.utcnow())
    with db.SessionLocal() as session:
        # Acquire transaction lock before the first SELECT, including SQLite.
        # PostgreSQL serializes release operations across all worker processes.
        _reg_lock(session,"I05_GLOBAL_RESEARCH_RELEASE_V1")
        artifact=_artifact_row(session,model_digest)
        report,_=_verify_artifact(artifact)
        scope=_scope(report["market"],report["symbol"],report["timeframe_seconds"])
        latest=_release_history(session,scope)
        if (latest[0].id if latest else None)!=expected_release_id:
            raise kb.KnowledgeConflict("Release concorrente ou revisão obsoleta")
        _no_open_contracts(session)
        model=kb._locked(session,model_knowledge_id,expected_model_revision)
        _model_match(model,report)
        if model.state!="VALIDATED":
            raise LearningError("Modelo sem validação I05 concluída")
        if (kb._utc(model.valid_from)>now
            or now>=kb._utc(model.valid_until)
            or now>=kb._utc(model.next_review_at)):
            raise LearningError("Modelo vencido na promoção")
        evidence=_verified_model_evidence(session,model,report)
        prior_champion=(latest[1]["champion_knowledge_id"] if latest else None)
        fallback=None
        if prior_champion is not None and _model_research_ready(
            session,prior_champion,now=now
        ):
            fallback=prior_champion
        kb._cas(session,model,expected_model_revision,state="ACTIVE",
                reason="I05_EXPLICIT_RESEARCH_APPROVAL",
                decision="I05_RESEARCH_ONLY_PROMOTION",now=now,
                digest=kb._snapshot_digest(evidence))
        release=_release_event(
            session,scope,champion_id=model.id,
            fallback_id=fallback,previous=latest,
            reason=reason,event="PROMOTED",now=now,
            last_check_epoch=report["period_to_epoch"])
        session.commit()
        return release


def get_research_release(*, market: str, symbol: str,
                         timeframe_seconds: int,
                         now: datetime | None = None) -> dict:
    """Fail safe if release artifact or current champion is not ACTIVE/fresh."""
    now=kb._utc(now or db.utcnow())
    scope=_scope(market,symbol,timeframe_seconds)
    with db.SessionLocal() as session:
        latest=_release_history(session,scope)
        if latest is None or kb._utc(latest[0].created_at)>now:
            return {
                "version":VERSION,"state":"NO_VALIDATED_RESEARCH_MODEL",
                "champion_knowledge_id":None,
                "fallback_knowledge_id":None,
                "execution_allowed":False,"approved_for_trading":False,
            }
        release=latest[1]
        champion=release["champion_knowledge_id"]
        if not _model_research_ready(session,champion,now=now):
            champion=None
        fallback=release["fallback_knowledge_id"]
        if not _model_research_ready(session,fallback,now=now):
            fallback=None
        if champion is None:
            champion=fallback
        return {
            "version":VERSION,
            "state":"RESEARCH_CHAMPION" if champion else "NO_VALIDATED_RESEARCH_MODEL",
            "release_id":latest[0].id,"generation":release["generation"],
            "champion_knowledge_id":champion,
            "fallback_knowledge_id":fallback,
            "release_event":release["event"],
            "execution_allowed":False,"approved_for_trading":False,
        }


def _find_model_artifact(session, model_id: int):
    model=session.get(db.IntelligenceKnowledge,model_id)
    if model is None or model.category!="MODEL":
        raise kb.KnowledgeUnavailable("Modelo de release ausente")
    kb._read_integrity(session,model)
    payload=json.loads(model.payload_json)
    report,_=_verify_artifact(_artifact_row(session,payload["model_id"]))
    _model_match(model,report)
    return report


def monitor_and_rollback(*, market: str, symbol: str,
                         timeframe_seconds: int, history: dict,
                         now: datetime | None = None) -> dict:
    """Public-only OOS drift check; auto-rollback swaps RESEARCH pointer.

    Two *non-overlapping* bad windows of at least 200 newly closed labels
    are required. Replays are ignored. Never changes strategies or orders.
    """
    if not learning_enabled():
        raise LearningError("Monitoramento I05 desabilitado")
    now=kb._utc(now or db.utcnow())
    states,epochs=_valid_history(history)
    scope=_scope(market,symbol,timeframe_seconds)
    if (history["market"],history["symbol"],history["timeframe_seconds"])!=scope:
        raise LearningError("Monitoramento não corresponde ao modelo público")
    if epochs[-1]>int(now.timestamp()):
        raise LearningError("Observação ainda não encerrada")
    with db.SessionLocal() as session:
        _reg_lock(session,_release_key(scope))
        latest=_release_history(session,scope)
        if latest is None or latest[1]["champion_knowledge_id"] is None:
            return {"state":"SAFE_FALLBACK","reason_code":"NO_CHAMPION",
                    "execution_allowed":False}
        release=latest[1]
        champion_id=release["champion_knowledge_id"]
        if not _model_research_ready(session,champion_id,now=now):
            return {"state":"SAFE_FALLBACK",
                    "reason_code":"STALE_OR_INVALID_CHAMPION",
                    "execution_allowed":False}
        artifact=_find_model_artifact(session,champion_id)
        previous_end=release.get("last_check_epoch") or artifact["period_to_epoch"]
        boundary=max(
            previous_end,
            artifact["train_until_epoch"]+EMBARGO*timeframe_seconds,
        )
        indices=[i for i in range(1,len(epochs)) if epochs[i]>boundary]
        if len(indices)<MIN_MONITOR_SAMPLES:
            return {
                "state":"AWAIT_NEW_PUBLIC_HOLDOUT",
                "observations_ready":len(indices),
                "execution_allowed":False,
            }
        model=artifact["artifact"]["transition_counts"]
        correct=sum(_predict(model,states[i-1])==states[i] for i in indices)
        baseline=sum(states[i-1]==states[i] for i in indices)
        accuracy=correct/len(indices)
        persistence=baseline/len(indices)
        bad=accuracy+.04<persistence or accuracy<.55
        streak=(release["bad_streak"]+1 if bad else 0)
        fallback=release["fallback_knowledge_id"]
        if fallback and not _model_research_ready(session,fallback,now=now):
            fallback=None
        do_rollback=streak>=BAD_WINDOWS_TO_ROLLBACK
        new_champion=(fallback if do_rollback else champion_id)
        if do_rollback:
            fallback=None  # Do not silently flip back to a degraded champion.
        next_event=_release_event(
            session,scope,champion_id=new_champion,
            fallback_id=fallback,previous=latest,
            reason=("I05_OOS_REGRESSION_AUTO_ROLLBACK" if do_rollback
                    else "I05_OOS_MONITOR_RECORDED"),
            event="AUTO_ROLLBACK" if do_rollback else "MONITORED",
            now=now,last_check_epoch=epochs[-1],
            bad_streak=0 if do_rollback else streak,
        )
        session.commit()
        return {
            "state":"RESEARCH_ROLLED_BACK" if do_rollback else "MONITORED",
            "release_id":next_event["id"],
            "bad_streak":next_event["bad_streak"],
            "holdout_samples":len(indices),
            "accuracy":round(accuracy,5),
            "baseline_accuracy":round(persistence,5),
            "champion_knowledge_id":new_champion,
            "fallback_knowledge_id":fallback,
            "execution_allowed":False,"approved_for_trading":False,
        }

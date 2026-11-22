"""Isolated upload-to-research adapter for historical OHLC.

No broker access, signals, orders or claims of verified market provenance.
"""
from __future__ import annotations
from datetime import datetime, timezone
from uuid import uuid4
import json
import re
import time
from sqlalchemy import Column, DateTime, Integer, String, Table, Text, insert, select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from . import stage4_general_homologation as core
from . import stage4_historical_research as research
from . import stage4_ohlc_quality as ohlc
from .research_signal_engine import research_lifecycle
from .learning_engine import _wilson_lower

REVISION = "nexora_stage4_user_ohlc_v1"
SOURCE = "USER_SUPPLIED_UNVERIFIED_OHLC"
VERSION = "uploaded_ohlc_descriptive_research_v1"
ORIGINS = ("DECLARED_REAL_UNVERIFIED", "SYNTHETIC_TEST")
CLASSES = ("FX", "EQUITY", "INDEX", "CRYPTO", "OTHER")
datasets = Table(
    "nexora_stage4_user_ohlc_datasets", core.meta,
    Column("id", String(40), primary_key=True),
    Column("symbol", String(24), nullable=False),
    Column("asset_class", String(16), nullable=False),
    Column("origin_type", String(32), nullable=False),
    Column("source_label", String(64), nullable=False),
    Column("granularity_seconds", Integer, nullable=False),
    Column("bar_count", Integer, nullable=False),
    Column("quality", String(18), nullable=False),
    Column("snapshot_sha256", String(64), nullable=False, unique=True),
    Column("bars_json", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

def _identity(row, bars):
    return {"symbol":row["symbol"], "asset_class":row["asset_class"],
            "origin_type":row["origin_type"], "source_label":row["source_label"],
            "granularity_seconds":row["granularity_seconds"], "bars":bars}

def _view(row):
    return {"id":row["id"], "symbol":row["symbol"],
            "asset_class":row["asset_class"], "origin_type":row["origin_type"],
            "source_label":row["source_label"],
            "granularity_seconds":row["granularity_seconds"],
            "bar_count":row["bar_count"], "quality":row["quality"],
            "snapshot_sha256":row["snapshot_sha256"],
            "created_at":research._iso(row["created_at"]),
            "synthetic":row["origin_type"] == "SYNTHETIC_TEST",
            "provenance_verified":False, "external_market_feed":False}

def _validate(payload):
    if not isinstance(payload, dict):
        raise ValueError("EXPECTED_JSON_OBJECT")
    symbol = payload.get("symbol")
    if not isinstance(symbol, str):
        raise ValueError("INVALID_SYMBOL")
    symbol = symbol.strip().upper()
    if re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{1,23}", symbol) is None:
        raise ValueError("INVALID_SYMBOL")
    asset_class = payload.get("asset_class", "OTHER")
    origin = payload.get("origin_type")
    label = payload.get("source_label")
    frame = payload.get("granularity_seconds")
    bars = payload.get("bars")
    if asset_class not in CLASSES or origin not in ORIGINS:
        raise ValueError("INVALID_SOURCE_CLASSIFICATION")
    if not isinstance(label, str) or not 3 <= len(label.strip()) <= 64:
        raise ValueError("SOURCE_LABEL_REQUIRED")
    if type(frame) is not int or frame not in ohlc.ALLOWED_GRANULARITIES:
        raise ValueError("UNSUPPORTED_GRANULARITY")
    if not isinstance(bars, list) or not 64 <= len(bars) <= 1000:
        raise ValueError("EXPECTED_64_TO_1000_CLOSED_BARS")
    if not all(isinstance(b, dict) and type(b.get("epoch")) is int for b in bars):
        raise ValueError("INVALID_BAR_EPOCH")
    as_of = bars[-1]["epoch"] + frame
    if as_of > int(time.time()) + frame:
        raise ValueError("FUTURE_BARS_NOT_ALLOWED")
    window = ohlc.inspect_window(
        ohlc.InMemoryOhlcArchive(bars), granularity_seconds=frame,
        count=len(bars), as_of_epoch=as_of)
    if window["quality"] != "COMPLETE":
        raise ValueError("OHLC_" + window["quality"] + "_" + "_".join(window["reason_codes"]))
    return {"symbol":symbol, "asset_class":asset_class,
            "origin_type":origin, "source_label":label.strip(),
            "granularity_seconds":frame, "bars":window["records"]}, window

def ingest_dataset(payload):
    identity, window = _validate(payload)
    digest = research._sha(identity)
    uid = uuid4().hex
    record = dict(id=uid, symbol=identity["symbol"],
                  asset_class=identity["asset_class"], origin_type=identity["origin_type"],
                  source_label=identity["source_label"],
                  granularity_seconds=identity["granularity_seconds"],
                  bar_count=len(identity["bars"]), quality=window["quality"],
                  snapshot_sha256=digest, bars_json=research._json(identity["bars"]),
                  created_at=research._when())
    with core._engine().begin() as conn:
        stmt = (pg_insert(datasets) if conn.dialect.name == "postgresql"
                else sqlite_insert(datasets) if conn.dialect.name == "sqlite" else None)
        if stmt is None:
            raise RuntimeError("UNSUPPORTED_DATABASE")
        conn.execute(stmt.values(**record).on_conflict_do_nothing(
            index_elements=["snapshot_sha256"]))
        stored = conn.execute(select(datasets).where(
            datasets.c.snapshot_sha256 == digest)).mappings().one()
    result = _view(stored)
    result["already_present"] = stored["id"] != uid
    return result

def list_assets(limit=50):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("INVALID_LIMIT")
    with core._engine().connect() as conn:
        rows = conn.execute(select(datasets).order_by(
            datasets.c.created_at.desc()).limit(limit)).mappings().all()
        total = conn.execute(select(func.count()).select_from(datasets)).scalar_one()
    return {"total":int(total), "assets":[_view(row) for row in rows],
            "provenance_verified":False, "market_feed_connected":False}

def get_asset(dataset_id):
    if not isinstance(dataset_id, str) or re.fullmatch(r"[0-9a-f]{32}",dataset_id) is None:
        raise ValueError("INVALID_DATASET_ID")
    with core._engine().connect() as conn:
        row = conn.execute(select(datasets).where(
            datasets.c.id == dataset_id)).mappings().one_or_none()
    if row is None:
        return None
    bars = json.loads(row["bars_json"])
    if len(bars) != row["bar_count"] or research._sha(
        _identity(row, bars)) != row["snapshot_sha256"]:
        raise RuntimeError("DATASET_INTEGRITY_ERROR")
    return _view(row),bars


def inspect_asset_quality(dataset_id):
    """Recompute data-quality evidence from the immutable archive, read-only.

    Integrity means the persisted bytes match the stored digest; it never
    establishes whether an external market supplied authentic prices.
    """
    stored = get_asset(dataset_id)
    if stored is None:
        return None
    asset, bars = stored
    step = asset["granularity_seconds"]
    first_epoch = bars[0]["epoch"]
    last_epoch = bars[-1]["epoch"]
    consecutive = [b["epoch"] - a["epoch"] for a, b in zip(bars, bars[1:])]
    missing = sum(max(0, (difference // step) - 1) for difference in consecutive
                  if difference > step and difference % step == 0)
    duplicates = sum(difference == 0 for difference in consecutive)
    invalid_interval = sum(difference <= 0 or difference % step != 0
                           for difference in consecutive)
    bad_ohlc = sum(
        not (
            isinstance(bar["high"], (int, float))
            and isinstance(bar["low"], (int, float))
            and all(isinstance(bar[field], (int, float))
                    for field in ("open", "close"))
            and bar["high"] >= max(bar["open"], bar["close"], bar["low"])
            and bar["low"] <= min(bar["open"], bar["close"], bar["high"])
        ) for bar in bars
    )
    if missing or duplicates or invalid_interval or bad_ohlc:
        raise RuntimeError("ARCHIVED_OHLC_QUALITY_INCONSISTENCY")
    return {
        "audit_version": "nexora-ohlc-quality-v1",
        "dataset_id": asset["id"],
        "symbol": asset["symbol"],
        "asset_class": asset["asset_class"],
        "source_label": asset["source_label"],
        "origin_type": asset["origin_type"],
        "synthetic": asset["synthetic"],
        "archive_digest_sha256": asset["snapshot_sha256"],
        "archive_integrity_verified": True,
        "provenance_verified": False,
        "external_market_feed_connected": False,
        "execution_allowed": False,
        "quality": "COMPLETE",
        "granularity_seconds": step,
        "observed_candles": len(bars),
        "expected_candles": ((last_epoch - first_epoch) // step) + 1,
        "missing_candles": missing,
        "duplicate_epochs": duplicates,
        "irregular_intervals": invalid_interval,
        "invalid_ohlc_rows": bad_ohlc,
        "first_closed_epoch": first_epoch,
        "last_closed_epoch": last_epoch,
        "coverage_seconds": last_epoch - first_epoch + step,
        "first_close": bars[0]["close"],
        "last_close": bars[-1]["close"],
        "minimum_close": min(bar["close"] for bar in bars),
        "maximum_close": max(bar["close"] for bar in bars),
        "limitations": [
            "Integrity verified against the archived SHA-256, not an external source",
            "Uploaded prices are not independently authenticated",
            "Quality measurements are descriptive only, not predictive signals",
            "No broker integration, trading orders, or financial execution",
        ],
    }


def research_dataset(dataset_id):
    source_pair = get_asset(dataset_id)
    if source_pair is None:
        raise LookupError("UNKNOWN_DATASET")
    asset,bars = source_pair
    uid,started,perf = uuid4().hex,research._when(),time.perf_counter()
    synthetic = asset["synthetic"]
    events=[]
    try:
        mark=research._when()
        window=ohlc.inspect_window(
            ohlc.InMemoryOhlcArchive(bars),
            granularity_seconds=asset["granularity_seconds"],
            count=len(bars), as_of_epoch=bars[-1]["epoch"]+asset["granularity_seconds"])
        if window["quality"]!="COMPLETE":
            raise ValueError("DATASET_QUALITY_CHANGED")
        a={"version":VERSION,"dataset_id":dataset_id,"asset":asset,
           "timeframes":[window],"total_bars":len(bars),
           "synthetic":synthetic,"provenance_verified":False}
        events.append(research._stage(uid,1,research.STAGES[0],
                      {"dataset_id":dataset_id,"sha256":asset["snapshot_sha256"]},a,mark))
        mark=research._when()
        b={"universe":[{"symbol":asset["symbol"],"asset_class":asset["asset_class"],
                        "quality":window["quality"],"bar_count":len(bars),
                        "timeframe_seconds":asset["granularity_seconds"],
                        "tradability_checked":False}],
           "classification_basis":"USER_SUPPLIED_METADATA_ONLY",
           "ranked_for_trading":False}
        events.append(research._stage(uid,2,research.STAGES[1],a,b,mark))
        mark=research._when()
        context=research._overview(window)
        c={"timeframe_summaries":[context],
           "indicators":{"mean_relative_bar_range":context["mean_relative_bar_range"],
                         "dispersion_of_close_changes":context["dispersion_of_close_changes"],
                         "mean_abs_relative_change":context["mean_abs_relative_change"]},
           "processed_bars":len(bars),"available_timeframes":[asset["granularity_seconds"]],
           "other_timeframes_synthesized":False}
        events.append(research._stage(uid,3,research.STAGES[2],b,c,mark))
        mark=research._when()
        knowledge_payload={"version":VERSION,"dataset_id":dataset_id,
                           "dataset_sha256":asset["snapshot_sha256"],
                           "context_sha256":research._sha(c),
                           "source":SOURCE,"origin_type":asset["origin_type"],
                           "synthetic":synthetic,"provenance_verified":False,
                           "knowledge_kind":"DESCRIPTIVE_HISTORICAL_WINDOW"}
        digest=research._sha(knowledge_payload)
        with core._engine().begin() as conn:
            stmt=(pg_insert(research.knowledge) if conn.dialect.name=="postgresql"
                  else sqlite_insert(research.knowledge)
                  if conn.dialect.name=="sqlite" else None)
            if stmt is None:
                raise RuntimeError("UNSUPPORTED_DATABASE")
            conn.execute(stmt.values(content_sha256=digest,source=SOURCE,
                         version=VERSION,payload_json=research._json(knowledge_payload),
                         first_seen_at=research._when()).on_conflict_do_nothing(
                         index_elements=["content_sha256"]))
            krow=conn.execute(select(research.knowledge).where(
                research.knowledge.c.content_sha256==digest)).mappings().one()
            if research._sha(json.loads(krow["payload_json"]))!=digest:
                raise RuntimeError("KNOWLEDGE_DIGEST_MISMATCH")
            d={"knowledge_id":krow["id"],"knowledge_sha256":digest,
               "deduplicated_by_content":True,"original_i04_service_executed":False}
            events.append(research._stage(uid,4,research.STAGES[3],c,d,mark))
            mark=research._when()
            split=research._split(window)
            e={"chronological_sample_comparison":split,
               "data_coverage_confidence_lower":round(_wilson_lower(len(bars),len(bars)),6),
               "comparison_method":"DESCRIPTIVE_70_30",
               "predictive_model_trained":False,"strategy_backtest_performed":False}
            events.append(research._stage(uid,5,research.STAGES[4],d,e,mark))
            mark=research._when()
            fingerprint=research._sha({"knowledge":digest,"evaluation":e,
                                       "source_snapshot":asset["snapshot_sha256"]})
            prior=conn.execute(select(research.steps.c.output_json)
                .join(research.runs,research.steps.c.run_id==research.runs.c.id)
                .where(research.steps.c.name==research.STAGES[5],
                       research.runs.c.source==SOURCE)
                .order_by(research.steps.c.id.desc()).limit(1)).scalar_one_or_none()
            lifecycle=research_lifecycle(
                previous_record={"fingerprint":json.loads(prior).get("fingerprint")}
                if prior else None,fingerprint=fingerprint,
                source_current=True,evidence_valid=True)
            f={"fingerprint":fingerprint,
               "evidence":["VALIDATED_CLOSED_OHLC","CONTIGUOUS_WINDOW",
                           "PERSISTED_DATASET_DIGEST","PERSISTED_KNOWLEDGE_DIGEST",
                           "CHRONOLOGICAL_SAMPLE_COMPARISON"],
               "lifecycle":lifecycle,"origin_type":asset["origin_type"],
               "provenance_verified":False,"synthetic":synthetic,
               "financial_executability":False}
            events.append(research._stage(uid,6,research.STAGES[5],e,f,mark))
            mark=research._when()
            report={"version":VERSION,"run_id":uid,"dataset_id":dataset_id,
                    "symbol":asset["symbol"],"asset_class":asset["asset_class"],
                    "source":SOURCE,"source_label":asset["source_label"],
                    "origin_type":asset["origin_type"],"synthetic":synthetic,
                    "provenance_verified":False,"state":"HISTORICAL_RESEARCH_RECORDED",
                    "bars_evaluated":len(bars),"timeframe_summaries":c["timeframe_summaries"],
                    "indicators":c["indicators"],"historical_validation":split,
                    "knowledge_id":d["knowledge_id"],"knowledge_sha256":digest,
                    "dataset_sha256":asset["snapshot_sha256"],
                    "evidence_fingerprint":fingerprint,"evidence":f["evidence"],
                    "lifecycle":lifecycle,
                    "limitations":["Uploaded provenance has not been independently verified",
                                   "Only the supplied timeframe was evaluated",
                                   "Descriptive chronological comparison; no predictive model",
                                   "Broker-oriented I01-I06 services were not executed",
                                   "No signals, orders or trading recommendations"],
                    "execution_allowed":False,"trade_recommendation":None}
            events.append(research._stage(uid,7,research.STAGES[6],f,report,mark))
            if [x["name"] for x in events]!=list(research.STAGES):
                raise RuntimeError("INCOMPLETE_RESEARCH_CHAIN")
            summary=research._sha(report)
            conn.execute(insert(research.runs).values(
                id=uid,version=VERSION,source=SOURCE,state="COMPLETE",
                started_at=started,finished_at=research._when(),
                duration_ms=max(0,round((time.perf_counter()-perf)*1000)),
                step_count=len(events),result_sha256=summary,
                report_json=research._json(report),
                reason="COMPLETED_UPLOADED_OHLC_RESEARCH"))
            for event in events:
                conn.execute(insert(research.steps).values(**event))
        return {"id":uid,"state":"COMPLETE","step_count":len(events),
                "result_sha256":summary,"source":SOURCE,"synthetic":synthetic,
                "dataset_id":dataset_id}
    except Exception as exc:
        try:
            with core._engine().begin() as conn:
                conn.execute(insert(research.runs).values(
                    id=uid,version=VERSION,source=SOURCE,state="ERROR",
                    started_at=started,finished_at=research._when(),
                    duration_ms=max(0,round((time.perf_counter()-perf)*1000)),
                    step_count=0,result_sha256=None,report_json=None,
                    reason=type(exc).__name__.upper()[:100]))
        except Exception:
            pass
        raise

"""Isolated, non-operational historical OHLC research.

Seven actual descriptive data transformations, without broker contracts,
trading signals, market rankings, financial returns or model promotion.
Reuses the existing OHLC validator, evidence lifecycle utility and a
statistical confidence-bound helper from existing I05 research code.
Not the original I01-I06 trading-oriented execution chain.
"""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean, median, pstdev
from collections import Counter
from uuid import uuid4
import hashlib
import json
import math
import time

from sqlalchemy import (
    Table, Column, Integer, DateTime, String, Text, ForeignKey,
    UniqueConstraint, select, insert, update, func,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from . import stage4_general_homologation as core
from . import stage4_ohlc_quality as ohlc
from .research_signal_engine import research_lifecycle
from .learning_engine import _wilson_lower

REVISION = "nexora_stage4_historical_research_v1"
VERSION = "independent_historical_research_v1"
SOURCE = ohlc.FIXTURE
DATASET = "FICTITIOUS_OHLC_SERIES_A"
FRAMES = ((300, 288), (900, 144), (3600, 96))
STAGES = (
    "HISTORICAL_OHLC",
    "DESCRIPTIVE_UNIVERSE",
    "CONTEXT_AND_TIMEFRAMES",
    "RESEARCH_KNOWLEDGE",
    "SPLIT_SAMPLE_STATISTICS",
    "EVIDENCE_AND_PROVENANCE",
    "RESEARCH_CONSOLIDATION",
)

runs = Table(
    "nexora_stage4_research_runs", core.meta,
    Column("id", String(40), primary_key=True),
    Column("version", String(64), nullable=False),
    Column("source", String(64), nullable=False),
    Column("state", String(24), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column("step_count", Integer, nullable=False),
    Column("result_sha256", String(64), nullable=True),
    Column("report_json", Text, nullable=True),
    Column("reason", String(100), nullable=False),
)
steps = Table(
    "nexora_stage4_research_steps", core.meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(40), ForeignKey("nexora_stage4_research_runs.id"), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("name", String(45), nullable=False),
    Column("state", String(24), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column("input_sha256", String(64), nullable=False),
    Column("output_sha256", String(64), nullable=False),
    Column("input_json", Text, nullable=False),
    Column("output_json", Text, nullable=False),
    Column("reason", String(100), nullable=False),
    UniqueConstraint("run_id", "ordinal", name="uq_stage4_research_run_ordinal"),
)
knowledge = Table(
    "nexora_stage4_research_knowledge", core.meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("content_sha256", String(64), unique=True, nullable=False),
    Column("source", String(64), nullable=False),
    Column("version", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
)

def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False, default=str)

def _sha(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()

def _when():
    return datetime.now(timezone.utc)

def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value

def _stage(run_id, ordinal, name, source, target, started, reason="PROCESSED"):
    if name != STAGES[ordinal - 1]:
        raise RuntimeError("Out-of-order research transformation")
    finished = _when()
    return {
        "run_id":run_id,"ordinal":ordinal,"name":name,"state":"OK",
        "started_at":started,"finished_at":finished,
        "duration_ms":max(0,round((finished-started).total_seconds()*1000)),
        "input_sha256":_sha(source),"output_sha256":_sha(target),
        "input_json":_json(source),"output_json":_json(target),
        "reason":reason,
    }

def _window(frame):
    granularity, count = frame
    result = ohlc.inspect_window(
        ohlc.DeterministicOhlcFixture(),
        granularity_seconds=granularity, count=count,
        as_of_epoch=ohlc.REFERENCE_ASOF,
    )
    if result["quality"] != "COMPLETE" or len(result["records"]) != count:
        raise ValueError("OHLC_NOT_CLOSED_CONTIGUOUS")
    return result

def _overview(window):
    bars = window["records"]
    opens = [b["open"] for b in bars]
    closes = [b["close"] for b in bars]
    changes = [(b-a)/a for a,b in zip(closes, closes[1:])]
    ranges = [(b["high"]-b["low"])/b["close"] for b in bars]
    return {
        "granularity_seconds":window["granularity_seconds"],
        "observations":len(bars),
        "first_closed_epoch":bars[0]["epoch"],
        "last_closed_epoch":bars[-1]["epoch"],
        "median_close":round(median(closes),8),
        "mean_relative_bar_range":round(mean(ranges),8),
        "dispersion_of_close_changes":round(pstdev(changes),8),
        "mean_abs_relative_change":round(mean(abs(x) for x in changes),8),
        "sample_open_to_close_difference":round(closes[-1]-opens[0],8),
        "analysis_kind":"DESCRIPTIVE_HISTORICAL_ONLY",
    }

def _split(window):
    closes=[b["close"] for b in window["records"]]
    changes=[abs((n-p)/p) for p,n in zip(closes,closes[1:])]
    split=int(len(changes)*.7)
    training, holdout=changes[:split],changes[split:]
    if len(training) < 20 or len(holdout) < 10:
        raise ValueError("INSUFFICIENT_SPLIT_SAMPLE")
    train=median(training)
    test=median(holdout)
    return {
        "granularity_seconds":window["granularity_seconds"],
        "training_observations":len(training),
        "holdout_observations":len(holdout),
        "training_median_abs_change":round(train,8),
        "holdout_median_abs_change":round(test,8),
        "absolute_median_difference":round(abs(test-train),8),
        "method":"CHRONOLOGICAL_DESCRIPTIVE_SPLIT_70_30",
        "predictive_model_trained":False,
        "financial_backtest":False,
    }

def create_research() -> dict:
    """Run and persist an independently useful historical study synchronously.

    A successful run writes all seven actual inputs/outputs, knowledge digest,
    and final report in one transaction. No state or network from trading app.
    """
    uid=uuid4().hex
    created=_when()
    perf=time.perf_counter()
    events=[]
    report=None
    reason="COMPLETED_DESCRIPTIVE_RESEARCH"
    try:
        source={"dataset":DATASET,"provider":SOURCE,
                "as_of_epoch":ohlc.REFERENCE_ASOF,
                "requested_frames":[{"seconds":f,"count":c} for f,c in FRAMES],
                "synthetic":True}
        mark=_when()
        windows=[_window(frame) for frame in FRAMES]
        a={"version":VERSION,"dataset":DATASET,"source":SOURCE,
           "synthetic":True,"timeframes":windows,
           "total_bars":sum(x["bar_count"] for x in windows)}
        events.append(_stage(uid,1,STAGES[0],source,a,mark))
        mark=_when()
        # Scanner examines only available datasets and their quality/coverage,
        # without selecting tradeable assets or asking for contract metadata.
        b={"version":VERSION,"universe":[{
            "name":DATASET,
            "coverage":[{"seconds":x["granularity_seconds"],
                         "count":x["bar_count"],"quality":x["quality"],
                         "gaps":x["gap_count"],
                         "sha256":x["sha256"]} for x in a["timeframes"]],
            "quality":"COMPLETE" if all(x["quality"]=="COMPLETE"
                                           for x in a["timeframes"]) else "REJECTED",
            "contract_metadata":"NOT_AVAILABLE",
            "tradeable_asset":False,
        }],"scanned_datasets":1,"ranked_for_trading":False}
        events.append(_stage(uid,2,STAGES[1],a,b,mark))
        mark=_when()
        if b["universe"][0]["quality"]!="COMPLETE":
            raise RuntimeError("RESEARCH_SOURCE_INCOMPLETE")
        c={"version":VERSION,
           "frames":[_overview(w) for w in a["timeframes"]],
           "source_digests":[w["sha256"] for w in a["timeframes"]],
           "indicators":"HISTORICAL_DISPERSION_RANGE_AND_VARIABILITY",
           "strategy_evaluation":False}
        events.append(_stage(uid,3,STAGES[2],b,c,mark))
        mark=_when()
        payload={"version":VERSION,"dataset":DATASET,
                 "context_sha256":_sha(c),
                 "source_sha256":_sha(b),
                 "synthetic":True,
                 "evidence_kind":"HISTORICAL_DESCRIPTIVE_STATISTICS"}
        kdigest=_sha(payload)
        with core._engine().begin() as conn:
            statement=(pg_insert(knowledge) if conn.dialect.name=="postgresql"
                       else sqlite_insert(knowledge) if conn.dialect.name=="sqlite"
                       else None)
            if statement is None:
                raise RuntimeError("DATABASE_DIALECT_UNSUPPORTED")
            conn.execute(statement.values(
                content_sha256=kdigest,source=SOURCE,version=VERSION,
                payload_json=_json(payload),first_seen_at=_when(),
            ).on_conflict_do_nothing(index_elements=["content_sha256"]))
            krow=conn.execute(select(knowledge).where(
                knowledge.c.content_sha256==kdigest)).mappings().one()
            if _sha(json.loads(krow["payload_json"])) != kdigest:
                raise RuntimeError("KNOWLEDGE_ARCHIVE_INTEGRITY_MISMATCH")
            d={"version":VERSION,"knowledge_id":krow["id"],
               "knowledge_sha256":kdigest,"stored":True,
               "knowledge_scope":"ISOLATED_HISTORICAL_DESCRIPTIVE_ARCHIVE",
               "i04_original_service_executed":False}
            events.append(_stage(uid,4,STAGES[3],c,d,mark))
            mark=_when()
            window_evaluations=[_split(w) for w in windows]
            observed=sum(w["bar_count"] for w in windows)
            # Reuse the pure existing I05 statistical bound, not its strategy
            # training/promotions or market-facing data collector.
            coverage_lower=round(_wilson_lower(observed,observed),6)
            e={"version":VERSION,"split_sample":window_evaluations,
               "closed_bars_evaluated":observed,
               "conservative_coverage_bound":coverage_lower,
               "i05_statistic_reused":"learning_engine._wilson_lower",
               "trained_market_strategy":False,
               "predicted_future_market_direction":False}
            events.append(_stage(uid,5,STAGES[4],d,e,mark))
            mark=_when()
            prior=conn.execute(select(runs.c.result_sha256).where(
                runs.c.state=="COMPLETE",
                runs.c.source==SOURCE,
            ).order_by(runs.c.finished_at.desc()).limit(1)).scalar_one_or_none()
            evidence_fingerprint=_sha({"knowledge":kdigest,
                                       "frames":[w["sha256"] for w in windows],
                                       "evaluation":e})
            # A same-source duplicate uses source content identity, not prior
            # report identity (which includes independently varying run id).
            prior_fp=conn.execute(select(steps.c.output_json)
                .where(steps.c.name=="EVIDENCE_AND_PROVENANCE")
                .order_by(steps.c.id.desc()).limit(1)).scalar_one_or_none()
            previous=None
            if prior_fp:
                last=json.loads(prior_fp)
                previous={"fingerprint":last.get("fingerprint")}
            lifecycle=research_lifecycle(
                previous_record=previous, fingerprint=evidence_fingerprint,
                source_current=True,evidence_valid=True)
            f={"version":VERSION,"fingerprint":evidence_fingerprint,
               "verified":["OHLC_CLOSED","NO_GAPS","REPRODUCIBLE_SOURCE",
                           "KNOWLEDGE_DIGEST","SPLIT_SAMPLE_COMPLETED"],
               "source":SOURCE,"synthetic":True,
               "knowledge_sha256":kdigest,
               "lifecycle":lifecycle,
               "evidence_calibration":"DATA_INTEGRITY_ONLY",
               "signal_or_order_generated":False,
               "i06_original_engine_executed":False}
            events.append(_stage(uid,6,STAGES[5],e,f,mark))
            mark=_when()
            report={"version":VERSION,"run_id":uid,"dataset":DATASET,
                    "source":SOURCE,"synthetic":True,
                    "state":"HISTORICAL_RESEARCH_RECORDED",
                    "timeframe_summaries":c["frames"],
                    "historical_validation":e["split_sample"],
                    "knowledge_id":d["knowledge_id"],
                    "knowledge_sha256":kdigest,
                    "evidence_fingerprint":f["fingerprint"],
                    "evidence":f["verified"],
                    "lifecycle":f["lifecycle"],
                    "limitations":[
                        "Synthetic OHLC, not real market prices",
                        "Research is descriptive, not a trading decision",
                        "Full I01-I06 broker-oriented modules are not connected",
                        "No financial model is trained or deployed",
                    ],
                    "execution_allowed":False,
                    "trade_recommendation":None}
            events.append(_stage(uid,7,STAGES[6],f,report,mark))
            if [x["name"] for x in events]!=list(STAGES):
                raise RuntimeError("INCOMPLETE_RESEARCH_PIPELINE")
            finished=_when()
            summary=_sha(report)
            conn.execute(insert(runs).values(
                id=uid,version=VERSION,source=SOURCE,state="COMPLETE",
                started_at=created,finished_at=finished,
                duration_ms=max(0,round((time.perf_counter()-perf)*1000)),
                step_count=len(events),result_sha256=summary,
                report_json=_json(report),reason=reason))
            for step in events:
                conn.execute(insert(steps).values(**step))
        return {"id":uid,"state":"COMPLETE","step_count":len(events),
                "result_sha256":summary,"source":SOURCE,"synthetic":True}
    except Exception as exc:
        safe=type(exc).__name__.upper()
        finished=_when()
        # A failed transaction never leaves an apparently complete report.
        # Best effort failure audit is separate from rolled-back data.
        try:
            with core._engine().begin() as conn:
                conn.execute(insert(runs).values(
                    id=uid,version=VERSION,source=SOURCE,state="ERROR",
                    started_at=created,finished_at=finished,
                    duration_ms=max(0,round((time.perf_counter()-perf)*1000)),
                    step_count=0,result_sha256=None,report_json=None,
                    reason=safe[:100]))
        except Exception:
            pass
        raise RuntimeError("Historical research failed: "+safe) from exc

def list_research(limit=20):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid research list limit")
    with core._engine().connect() as conn:
        records=conn.execute(select(runs).order_by(
            runs.c.finished_at.desc(),runs.c.id.desc()).limit(limit)).mappings().all()
        total=conn.execute(select(func.count()).select_from(runs)).scalar_one()
    return {"total":int(total),"runs":[{
        "id":r["id"],"state":r["state"],"version":r["version"],
        "source":r["source"],"synthetic":True,
        "step_count":r["step_count"],"reason":r["reason"],
        "started_at":_iso(r["started_at"]), "finished_at":_iso(r["finished_at"]),
        "duration_ms":r["duration_ms"],
        "result_sha256":r["result_sha256"],
    } for r in records]}

def get_research(run_id):
    if not isinstance(run_id,str) or len(run_id)!=32 or any(
        c not in "0123456789abcdef" for c in run_id):
        raise ValueError("Invalid research identifier")
    with core._engine().connect() as conn:
        found=conn.execute(select(runs).where(runs.c.id==run_id)).mappings().one_or_none()
        if found is None:
            return None
        trace=conn.execute(select(steps).where(steps.c.run_id==run_id)
            .order_by(steps.c.ordinal)).mappings().all()
    report=json.loads(found["report_json"]) if found["report_json"] else None
    if report is not None and _sha(report)!=found["result_sha256"]:
        raise RuntimeError("Research report integrity error")
    output=[]
    for item in trace:
        inp=json.loads(item["input_json"])
        out=json.loads(item["output_json"])
        if _sha(inp)!=item["input_sha256"] or _sha(out)!=item["output_sha256"]:
            raise RuntimeError("Research stage integrity error")
        output.append({
            "ordinal":item["ordinal"],"name":item["name"],
            "state":item["state"],"duration_ms":item["duration_ms"],
            "started_at":_iso(item["started_at"]),
            "finished_at":_iso(item["finished_at"]),
            "input_sha256":item["input_sha256"],
            "output_sha256":item["output_sha256"],
            "input":inp,"output":out,"reason":item["reason"],
        })
    return {
        "id":found["id"],"version":found["version"],
        "source":found["source"],"synthetic":True,
        "state":found["state"],"reason":found["reason"],
        "started_at":_iso(found["started_at"]),
        "finished_at":_iso(found["finished_at"]),
        "duration_ms":found["duration_ms"],
        "step_count":found["step_count"],
        "result_sha256":found["result_sha256"],
        "report":report,"stages":output,
        "original_trading_modules_integrated":False,
    }

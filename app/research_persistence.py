"""Isolated daily, read-only 1HZ100V research. No broker trading calls.

Research records use the existing DATABASE_URL (production Postgres/Supabase).
The background task never blocks application startup or changes AutoPilot.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text, select

from . import db
from .single_asset_study import run_single_asset_study

logger = logging.getLogger("nexora.research")
RESEARCH_KEY = "1HZ100V_M5_30D_V1"


class HistoricalResearchReport(db.Base):
    __tablename__ = "historical_research_reports"
    id = Column(Integer, primary_key=True)
    research_key = Column(String(60), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    period_from = Column(DateTime(timezone=True), nullable=False)
    period_to = Column(DateTime(timezone=True), nullable=False)
    payload_json = Column(Text, nullable=False)


def latest_study():
    with db.SessionLocal() as session:
        row = session.scalars(
            select(HistoricalResearchReport)
            .where(HistoricalResearchReport.research_key == RESEARCH_KEY)
            .order_by(HistoricalResearchReport.created_at.desc())
            .limit(1)
        ).first()
        return json.loads(row.payload_json) if row else None


MULTI_RESEARCH_KEY = "1HZ100V_M1_M15_30D_V1"


def latest_multitimeframe_study():
    with db.SessionLocal() as session:
        row = session.scalars(
            select(HistoricalResearchReport)
            .where(HistoricalResearchReport.research_key == MULTI_RESEARCH_KEY)
            .order_by(HistoricalResearchReport.created_at.desc())
            .limit(1)
        ).first()
        return json.loads(row.payload_json) if row else None


def persist_multitimeframe_study(report):
    """Store immutable research snapshots; never alter broker orders."""
    with db.SessionLocal() as session:
        session.add(HistoricalResearchReport(
            research_key=MULTI_RESEARCH_KEY,
            created_at=datetime.now(timezone.utc),
            period_from=datetime.fromisoformat(report["period_from"]),
            period_to=datetime.fromisoformat(report["period_to"]),
            payload_json=json.dumps(report, separators=(",", ":"), allow_nan=False)
        ))
        session.commit()


PULLBACK_KEY = "1HZ100V_PULLBACK_M1_M3_V1"


def latest_pullback_study():
    with db.SessionLocal() as session:
        row = session.scalars(
            select(HistoricalResearchReport)
            .where(HistoricalResearchReport.research_key == PULLBACK_KEY)
            .order_by(HistoricalResearchReport.created_at.desc())
            .limit(1)
        ).first()
        return json.loads(row.payload_json) if row else None


def persist_pullback_study(report):
    with db.SessionLocal() as session:
        session.add(HistoricalResearchReport(
            research_key=PULLBACK_KEY,
            created_at=datetime.now(timezone.utc),
            period_from=datetime.fromisoformat(report["period_from"]),
            period_to=datetime.fromisoformat(report["period_to"]),
            payload_json=json.dumps(report, separators=(",", ":"), allow_nan=False)
        ))
        session.commit()


def audit_summary(report):
    """Compact transparent diagnostics; does not promote any trading setup."""
    rows = report.get("results", [])
    eligible = [r for r in rows if r["holdout"]["signals"] >= 100]
    def view(r):
        h = r["holdout"]
        return {"tf":r["timeframe"],"expiry":r["expiry_minutes"],
                "strategy":r["strategy"],"signals":h["signals"],
                "win_rate":h["win_rate"],"lower95":h["wilson_95_lower"],
                "pnl_hypothetical":h["pnl_per_1_usd_at_88pct"],
                "training_signals":r["training"]["signals"],
                "training_rate":r["training"]["win_rate"]}
    groups = {}
    for expiry in (1,3,5,15):
        subset = [r for r in eligible if r["expiry_minutes"] == expiry]
        groups[str(expiry)] = {
            "tested": len([r for r in rows if r["expiry_minutes"] == expiry]),
            "eligible": len(subset),
            "best_holdout_observed": view(max(subset, key=lambda r:r["holdout"]["win_rate"]))
                if subset else None}
    smc = [view(r) for r in rows if r["strategy"]=="smc_sweep_choch_fvg_50"]
    flagged = [view(r) for r in rows if r.get("passed_exploratory_threshold")]
    best_by_strategy = {}
    for key in sorted(set(r["strategy"] for r in rows)):
        subset = [r for r in eligible if r["strategy"]==key]
        best_by_strategy[key] = view(max(subset,key=lambda r:r["holdout"]["win_rate"])) if subset else None
    return {
        "models_tested":len(rows), "m1_candles":report.get("m1_candles"),
        "coverage_pct":report.get("coverage_pct"),
        "training_selected":report.get("training_selected"),
        "by_expiry":groups,"by_strategy_observed":best_by_strategy,
        "smc_all":smc,"exploratory_flags":flagged,
        "note":"Post-hoc observed maxima are descriptive, not unbiased strategy validation."
    }


def _persist(report):
    with db.SessionLocal() as session:
        session.add(HistoricalResearchReport(
            research_key=RESEARCH_KEY,
            created_at=datetime.now(timezone.utc),
            period_from=datetime.fromisoformat(report["period_from"]),
            period_to=datetime.fromisoformat(report["period_to"]),
            payload_json=json.dumps(report, separators=(",", ":"), allow_nan=False),
        ))
        session.commit()


async def research_loop():
    """Run once when stale and recheck daily; failure leaves AutoPilot unaffected."""
    await asyncio.sleep(75)  # Avoid competing with the initial market self-check.
    while True:
        try:
            # Retain only useful research snapshots; never delete trade/financial records.
            from .research_retention import cleanup_research_reports
            removed = await asyncio.to_thread(cleanup_research_reports)
            if removed:
                logger.info("Research retention removed %s old snapshots", removed)
            existing = await asyncio.to_thread(latest_study)
            last = datetime.fromisoformat(existing["generated_at"]) if existing and existing.get("generated_at") else None
            stale = not last or datetime.now(timezone.utc) - last > timedelta(hours=24)
            if stale:
                report = await asyncio.wait_for(run_single_asset_study(), timeout=240)
                report["generated_at"] = datetime.now(timezone.utc).isoformat()
                await asyncio.to_thread(_persist, report)
                logger.warning("Research 1HZ100V complete: %s", json.dumps({
                    "candles": report["candles"],
                    "period_from": report["period_from"],
                    "period_to": report["period_to"],
                    "strategies": report["strategies"],
                    "orders_sent": 0,
                    "execution_enabled": False
                }, separators=(",", ":")))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Research 1HZ100V unavailable: %s", type(exc).__name__)
        await asyncio.sleep(3600)

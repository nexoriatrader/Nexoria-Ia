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

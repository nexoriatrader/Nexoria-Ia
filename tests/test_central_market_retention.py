"""Shared public-market data and bounded historical report retention."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app import db, shared_market_store, research_retention
from app.research_persistence import HistoricalResearchReport


def _fixture(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", factory)
    return engine, factory


def test_shared_market_db_snapshot_reused_and_expires(monkeypatch):
    engine, _ = _fixture(monkeypatch)
    monkeypatch.setenv("NEXORA_DISTRIBUTED_MARKET_CACHE_ENABLED", "true")
    candles = {"candles": [{"epoch": 120, "open": "1", "high": "2",
                            "low": "0", "close": "1.3"}]}
    assert shared_market_store.save_snapshot(
        "R_100", 60, 165, 3, candles, 12, now=181)
    assert shared_market_store.get_snapshot("R_100", 60, 165, 3, 182) == candles
    assert shared_market_store.get_snapshot("R_75", 60, 165, 3, 182) is None
    assert shared_market_store.get_snapshot("R_100", 60, 165, 3, 194) is None
    assert shared_market_store.prune_expired(now=194) == 1
    assert shared_market_store.prune_expired(now=194) == 0
    engine.dispose()


def test_stale_shared_candles_never_saved(monkeypatch):
    engine, factory = _fixture(monkeypatch)
    monkeypatch.setenv("NEXORA_DISTRIBUTED_MARKET_CACHE_ENABLED", "true")
    assert not shared_market_store.save_snapshot(
        "R_100", 60, 165, 3,
        {"candles": [{"epoch": 60}]}, 12, now=181)
    with factory() as session:
        assert session.query(shared_market_store.PublicCandleSnapshot).count() == 0
    engine.dispose()


def test_retention_preserves_latest_per_strategy_and_financial_tables(monkeypatch):
    engine, factory = _fixture(monkeypatch)
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    with factory() as session:
        for key in ("study_a", "study_b"):
            for i in range(13):
                stamp = now - timedelta(days=12-i)
                session.add(HistoricalResearchReport(
                    research_key=key, created_at=stamp,
                    period_from=stamp, period_to=stamp, payload_json="{}"))
        session.commit()
    deleted = research_retention.cleanup_research_reports(now=now, session_factory=factory)
    assert deleted == 14  # keep newest 6 of each key (7 older than 7 days)
    with factory() as session:
        for key in ("study_a", "study_b"):
            rows = session.scalars(select(HistoricalResearchReport)
                .where(HistoricalResearchReport.research_key == key)).all()
            assert len(rows) == 6
        assert research_retention.cleanup_research_reports(now=now, session_factory=factory) == 0
    engine.dispose()


def test_retention_keeps_minimum_three_even_when_all_are_old(monkeypatch):
    engine, factory = _fixture(monkeypatch)
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    with factory() as session:
        for i in range(5):
            created = now - timedelta(days=30+i)
            session.add(HistoricalResearchReport(
                research_key="legacy", created_at=created,
                period_from=created, period_to=created, payload_json="{}"))
        session.commit()
    assert research_retention.cleanup_research_reports(now=now, session_factory=factory) == 2
    with factory() as session:
        assert session.query(HistoricalResearchReport).count() == 3
    engine.dispose()

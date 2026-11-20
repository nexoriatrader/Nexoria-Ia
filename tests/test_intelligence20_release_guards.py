"""Read-only release candidate gates. No remote actions or financial orders."""
from __future__ import annotations

import inspect
from pathlib import Path

from fastapi.testclient import TestClient

from app import db, main
from app.intelligence2 import (
    market_data, market_scanner, context_engine, timeframe_engine, knowledge_base
)

ROOT = Path(__file__).resolve().parents[1]


def test_i01_to_i04_use_one_app_and_authoritative_public_adapters():
    assert callable(market_data.read_closed_window)
    assert callable(market_scanner.scan_market_opportunities)
    assert callable(context_engine.analyze_context)
    assert callable(timeframe_engine.enrich_i02_report)
    assert callable(knowledge_base.retrieve_active)
    assert "shared_candle_request" in inspect.getsource(market_data.read_closed_window)
    assert "market_data.read_closed_window" in inspect.getsource(timeframe_engine._analyze_one)
    assert "scan_intelligence_markets" in inspect.getsource(main.asset_scanner_route)
    assert "scan_intelligence_context" in inspect.getsource(main.asset_scanner_route)
    assert "get_knowledge" in inspect.getsource(main.knowledge_lineage_route)
    assert knowledge_base.VERSION == "intelligence2_knowledge_base_v1"


def test_financial_execution_stays_independent_of_intelligence(monkeypatch):
    monkeypatch.setenv("NEXORA_REAL_AUTOPILOT_ENABLED","false")
    monkeypatch.setenv("NEXORA_REAL_RELEASE_APPROVED","false")
    monkeypatch.setenv("NEXORA_DEMO_BUY_ENABLED","false")
    monkeypatch.delenv("NEXORA_INTELLIGENCE_KNOWLEDGE_ENABLED",raising=False)
    assert knowledge_base.enabled() is False
    route_source=inspect.getsource(main.knowledge_retrieval_route)
    assert "buy" not in route_source and "get_knowledge" not in route_source
    assert not any(
        term in inspect.getsource(module)
        for module in (market_data, context_engine, timeframe_engine)
        for term in ("buy_real_contract(", "demo_buy(", "reserve_real_order(")
    )
    client=TestClient(main.app)
    try:
        for path in ("/api/research/asset-scanner?version=2",
                     "/api/research/asset-scanner?version=3",
                     "/api/research/knowledge?category=MARKET&market=forex"):
            assert client.get(path).status_code == 401
    finally:
        client.close()


def test_migration_is_formal_only_and_has_backup_gate():
    sql=(ROOT/"migrations"/"I04_KNOWLEDGE_BASE_V1_POSTGRES.sql").read_text()
    guide=(ROOT/"docs"/"INTELLIGENCE_20_RELEASE_SEQUENCE.md").read_text()
    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert "i04_knowledge_base_v1" in sql
    for marker in ("pg_dump", "rollback", "snapshot", "restore",
                   "NEXORA_INTELLIGENCE_KNOWLEDGE_ENABLED=false",
                   "P0/F01", "main", "GitHub"):
        assert marker.lower() in guide.lower()
    pg_gate=inspect.getsource(db.init_db)
    assert "verify_postgresql_schema(connection)" in pg_gate
    assert pg_gate.index("verify_postgresql_schema(connection)") < pg_gate.index(
        "Base.metadata.create_all(engine)")

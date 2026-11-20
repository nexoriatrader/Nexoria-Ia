"""Main-based research candidate safety: opt-in, legacy behavior, no orders."""
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
import pytest
from app import main
from app.intelligence2 import market_data,market_scanner,context_engine,timeframe_engine

def test_research_opt_in_is_off_by_default(monkeypatch):
    monkeypatch.delenv("NEXORA_INTELLIGENCE_READONLY_ENABLED",raising=False)
    monkeypatch.setattr(main,"active_user",lambda request,sid:object())
    monkeypatch.setattr(main,"scan_intelligence_markets",AsyncMock(
        side_effect=AssertionError("disabled scanner must not access market")))
    monkeypatch.setattr(main,"scan_intelligence_context",AsyncMock(
        side_effect=AssertionError("disabled scanner must not access market")))
    with TestClient(main.app) as client:
        assert client.get("/api/research/asset-scanner?version=2").status_code==503
        assert client.get("/api/research/asset-scanner?version=3").status_code==503
        assert client.get("/api/research/asset-scanner?version=9").status_code==422
        assert client.get("/api/research/asset-scanner?version=3&expiry_minutes=2").status_code==422

def test_existing_v1_scanner_unchanged_when_research_off(monkeypatch):
    monkeypatch.delenv("NEXORA_INTELLIGENCE_READONLY_ENABLED",raising=False)
    monkeypatch.setattr(main,"active_user",lambda request,sid:object())
    monkeypatch.setattr(main,"_asset_scanner_cache",None)
    monkeypatch.setattr(main,"_asset_scanner_cache_time",0.0)
    existing=AsyncMock(return_value={"state":"RESEARCH_ONLY","items":[],"orders_sent":0})
    monkeypatch.setattr(main,"scan_assets",existing)
    with TestClient(main.app) as client:
        response=client.get("/api/research/asset-scanner")
        assert response.status_code==200
        assert response.json()["cached"] is False
        assert client.get("/api/research/asset-scanner") .json()["cached"] is True
    existing.assert_awaited_once()

def test_research_modules_never_grant_execution():
    import inspect
    for module in (market_data, market_scanner, context_engine, timeframe_engine):
        source=inspect.getsource(module)
        for forbidden in ("buy_real_contract(", "demo_buy(", "perform_demo_buy(", 
                          "reserve_real_order(", "apply_migration("):
            assert forbidden not in source
    assert timeframe_engine.VERSION.startswith("intelligence2_")

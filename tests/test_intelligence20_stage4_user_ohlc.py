"""End-to-end isolated uploaded OHLC HTTP + persistence, never any broker calls."""
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
import pytest

from app.intelligence2 import stage4_general_homologation as core
from app.intelligence2 import stage4_ohlc_quality as ohlc
from app.intelligence2 import stage4_user_ohlc as uploads
from app.intelligence2 import stage4_historical_research as research

KEY = "local-isolated-stage4-test-key-" + "x" * 34
HEADERS = {"X-NEXORA-STAGE4-KEY": KEY}


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "research.sqlite"),
                           connect_args={"check_same_thread":False})
    core.meta.create_all(engine)
    monkeypatch.setattr(core, "_engine", lambda: engine)
    monkeypatch.setenv("NEXORA_STAGE4_ACCESS_TOKEN", KEY)
    yield engine
    engine.dispose()


def sample(origin="SYNTHETIC_TEST"):
    bars = ohlc.DeterministicOhlcFixture().load(
        granularity_seconds=300, count=96, as_of_epoch=ohlc.REFERENCE_ASOF)
    return {
        "symbol":"OFFLINE_FIXTURE_A", "asset_class":"OTHER",
        "origin_type":origin, "source_label":"Offline test fixture only",
        "granularity_seconds":300, "bars":bars,
    }


def test_uploaded_asset_and_seven_stage_research_survive_reconnection(
        isolated_db, monkeypatch, tmp_path):
    client = TestClient(core.app)  # no lifespan, no ingest-loop
    assert client.post("/api/homologation/assets",json=sample()).status_code==401
    assert client.get("/api/homologation/assets").status_code==401
    assert client.post("/api/homologation/research/runs/uploaded/"+"a"*32).status_code==401
    uploaded=client.post("/api/homologation/assets",json=sample(),headers=HEADERS)
    assert uploaded.status_code==200,uploaded.text
    meta=uploaded.json()
    assert meta["quality"]=="COMPLETE" and meta["bar_count"]==96
    assert meta["synthetic"] is True and meta["provenance_verified"] is False
    duplicate=client.post("/api/homologation/assets",json=sample(),headers=HEADERS)
    assert duplicate.json()["already_present"] is True
    assert duplicate.json()["id"]==meta["id"]
    catalog=client.get("/api/homologation/assets",headers=HEADERS)
    assert catalog.status_code==200 and catalog.json()["total"]==1
    record=client.get("/api/homologation/assets/"+meta["id"],headers=HEADERS)
    assert record.status_code==200 and len(record.json()["bars"])==96
    response=client.post("/api/homologation/research/runs/uploaded/"+meta["id"],
                         headers=HEADERS)
    assert response.status_code==200,response.text
    run=response.json()
    assert run["state"]=="COMPLETE" and run["step_count"]==7
    detail=client.get("/api/homologation/research/runs/"+run["id"],headers=HEADERS)
    assert detail.status_code==200,detail.text
    report=detail.json()
    assert report["result_sha256"]==run["result_sha256"]
    assert [item["name"] for item in report["stages"]]==list(research.STAGES)
    assert report["report"]["dataset_id"]==meta["id"]
    assert report["report"]["provenance_verified"] is False
    assert report["report"]["execution_allowed"] is False
    assert report["report"]["trade_recommendation"] is None
    assert report["report"]["historical_validation"]["holdout_observations"]>10
    runs=client.get("/api/homologation/research/runs",headers=HEADERS)
    assert runs.status_code==200 and runs.json()["total"]==1
    assert runs.json()["runs"][0]["synthetic"] is True

    # Simulate service worker restart: discard connection pool and reopen same DB.
    isolated_db.dispose()
    replacement=create_engine("sqlite:///" + str(tmp_path/"research.sqlite"),
                              connect_args={"check_same_thread":False})
    monkeypatch.setattr(core,"_engine",lambda:replacement)
    try:
        assert client.get("/api/homologation/assets",headers=HEADERS).json()["total"]==1
        restored=client.get("/api/homologation/research/runs/"+run["id"],
                            headers=HEADERS)
        assert restored.status_code==200
        assert restored.json()["result_sha256"]==run["result_sha256"]
        assert len(restored.json()["stages"])==7
    finally:
        replacement.dispose()


def test_invalid_bars_are_rejected_without_persistence(isolated_db):
    client=TestClient(core.app)
    payload=sample()
    payload["bars"][0]["high"]=-1.0
    result=client.post("/api/homologation/assets",json=payload,headers=HEADERS)
    assert result.status_code==422,result.text
    assert client.get("/api/homologation/assets",headers=HEADERS).json()["total"]==0
    assert client.post("/api/homologation/research/runs/uploaded/"+"0"*32,
                       headers=HEADERS).status_code==404
    assert not any("/buy" in route.path or "/orders" in route.path
                   for route in core.app.routes)


def test_origin_markers_cannot_claim_verified_prices(isolated_db):
    client=TestClient(core.app)
    payload=sample("DECLARED_REAL_UNVERIFIED")
    created=client.post("/api/homologation/assets",json=payload,headers=HEADERS)
    assert created.status_code==200
    assert created.json()["synthetic"] is False
    assert created.json()["provenance_verified"] is False
    started=client.post("/api/homologation/research/runs/uploaded/"+
                        created.json()["id"],headers=HEADERS)
    assert started.status_code==200,started.text
    stored=client.get("/api/homologation/research/runs/"+started.json()["id"],
                      headers=HEADERS).json()
    assert stored["synthetic"] is False
    assert stored["report"]["origin_type"]=="DECLARED_REAL_UNVERIFIED"
    assert stored["report"]["provenance_verified"] is False
    assert "No signals, orders or trading recommendations" in stored["report"]["limitations"]

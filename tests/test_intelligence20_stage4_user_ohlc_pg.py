"""Disposable PostgreSQL end-to-end OHLC research, no production access."""
import os
import pytest
from sqlalchemy import create_engine
from fastapi.testclient import TestClient
from app.intelligence2 import stage4_general_homologation as core
from app.intelligence2 import stage4_ohlc_quality as ohlc

def test_persisted_authenticated_user_research_on_disposable_postgresql(monkeypatch):
    url=os.environ["DATABASE_URL"]
    if not ("nexora_public_test" in url and "127.0.0.1:5432" in url):
        pytest.skip("Requires explicitly named disposable CI PostgreSQL; never run on other DBs")
    secret="isolated-disposable-pg-test-" + "x"*40
    monkeypatch.setenv("NEXORA_STAGE4_ACCESS_TOKEN",secret)
    headers={"X-NEXORA-STAGE4-KEY":secret}
    engine=create_engine(url)
    core.meta.create_all(engine)  # Allowed only on GitHub Actions ephemeral PostgreSQL
    monkeypatch.setattr(core,"_engine",lambda:engine)
    bars=ohlc.DeterministicOhlcFixture().load(
        granularity_seconds=900,count=80,as_of_epoch=ohlc.REFERENCE_ASOF)
    payload={"symbol":"CI_SYNTHETIC_PG","asset_class":"OTHER",
             "origin_type":"SYNTHETIC_TEST",
             "source_label":"Disposable CI synthetic OHLC",
             "granularity_seconds":900,"bars":bars}
    client=TestClient(core.app)  # no lifespan / sensor worker
    assert client.post("/api/homologation/assets",json=payload).status_code==401
    saved=client.post("/api/homologation/assets",json=payload,headers=headers)
    assert saved.status_code==200,saved.text
    uid=saved.json()["id"]
    started=client.post("/api/homologation/research/runs/uploaded/"+uid,
                        headers=headers)
    assert started.status_code==200,started.text
    key=started.json()["id"]
    digest=started.json()["result_sha256"]
    assert started.json()["step_count"]==7
    engine.dispose()
    replacement=create_engine(url)
    monkeypatch.setattr(core,"_engine",lambda:replacement)
    try:
        assert client.get("/api/homologation/assets/"+uid,headers=headers).status_code==200
        history=client.get("/api/homologation/research/runs",headers=headers)
        assert history.status_code==200
        assert any(item["id"]==key for item in history.json()["runs"])
        restored=client.get("/api/homologation/research/runs/"+key,headers=headers)
        assert restored.status_code==200
        data=restored.json()
        assert data["result_sha256"]==digest
        assert len(data["stages"])==7
        assert data["report"]["execution_allowed"] is False
    finally:
        replacement.dispose()

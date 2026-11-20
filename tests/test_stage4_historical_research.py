"""Integrated non-operational historical research with real stage handoffs."""
from datetime import datetime, timezone
from sqlalchemy import create_engine
from fastapi.testclient import TestClient
from app.intelligence2 import stage4_general_homologation as app
from app.intelligence2 import stage4_historical_research as study

TOKEN = "test-only-strong-secret-32-characters-long"

def setup(monkeypatch,tmp_path):
    engine=create_engine("sqlite:///"+str(tmp_path/"historical_research.sqlite"))
    app.meta.create_all(engine)
    monkeypatch.setattr(app,"_engine",lambda:engine)
    monkeypatch.setenv("NEXORA_STAGE4_ACCESS_TOKEN",TOKEN)
    return engine

def test_full_historical_study_with_real_transforms_and_durable_restart(monkeypatch,tmp_path):
    db=setup(monkeypatch,tmp_path)
    first=study.create_research()
    again=study.create_research()
    assert first["state"]=="COMPLETE" and again["state"]=="COMPLETE"
    assert first["id"]!=again["id"]
    assert first["step_count"]==7
    view=study.get_research(first["id"])
    assert [x["name"] for x in view["stages"]]==list(study.STAGES)
    assert all(x["input_sha256"] and x["output_sha256"] and x["state"]=="OK"
               for x in view["stages"])
    assert view["stages"][0]["output"]["total_bars"]==sum(n for _,n in study.FRAMES)
    assert view["stages"][1]["output"]["scanned_datasets"]==1
    assert len(view["stages"][2]["output"]["frames"])==3
    assert view["stages"][3]["output"]["stored"] is True
    assert view["stages"][4]["output"]["trained_market_strategy"] is False
    assert view["stages"][5]["output"]["signal_or_order_generated"] is False
    assert view["report"]["trade_recommendation"] is None
    assert study.get_research(again["id"])["report"]["lifecycle"]["lifecycle"]=="UNCHANGED"
    first_sha=view["result_sha256"]
    assert study.list_research()["total"]==2
    db.dispose()
    reopened=create_engine("sqlite:///"+str(tmp_path/"historical_research.sqlite"))
    monkeypatch.setattr(app,"_engine",lambda:reopened)
    assert study.get_research(first["id"])["result_sha256"]==first_sha
    assert study.list_research()["total"]==2
    reopened.dispose()

def test_authenticated_post_history_detail_and_bad_ids(monkeypatch,tmp_path):
    db=setup(monkeypatch,tmp_path)
    client=TestClient(app.app)
    assert client.post("/api/homologation/research/runs").status_code==401
    assert client.get("/api/homologation/research/runs").status_code==401
    hdr={"X-NEXORA-STAGE4-KEY":TOKEN}
    started=client.post("/api/homologation/research/runs",headers=hdr)
    assert started.status_code==200,started.text
    uid=started.json()["id"]
    detail=client.get("/api/homologation/research/runs/"+uid,headers=hdr)
    assert detail.status_code==200
    assert detail.json()["step_count"]==7
    assert len(detail.json()["stages"])==7
    assert detail.json()["result_sha256"]==started.json()["result_sha256"]
    assert client.get("/api/homologation/research/runs",headers=hdr).json()["total"]==1
    assert client.get("/api/homologation/research/runs/not-an-id",headers=hdr).status_code==422
    assert client.get("/api/homologation/research/runs/"+"a"*32,headers=hdr).status_code==404
    db.dispose()

def test_validator_failure_does_not_persist_success(monkeypatch,tmp_path):
    db=setup(monkeypatch,tmp_path)
    from app.intelligence2 import stage4_ohlc_quality as ohlc
    original=ohlc.inspect_window
    def invalid(*args,**kwargs):
        val=original(*args,**kwargs);val["quality"]="INVALID";return val
    monkeypatch.setattr(ohlc,"inspect_window",invalid)
    import pytest
    with pytest.raises(RuntimeError,match="Historical research failed"):
        study.create_research()
    records=study.list_research()
    assert records["total"]==1
    assert records["runs"][0]["state"]=="ERROR"
    assert records["runs"][0]["step_count"]==0
    db.dispose()

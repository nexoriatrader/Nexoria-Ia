"""End-to-end historical OHLC research, tenant isolation, replay, no broker actions."""
import copy
import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app import db, main
from app.intelligence2.historical_research import (
    ResearchInputError, build_report, normalize_input, summary,
)
from app.intelligence2.research_store import MAX_PER_USER
from app.research_persistence import HistoricalResearchReport
from app.security import encrypt, hashsecret


def sample(count=1140):
    # Synthetic TEST FIXTURE only; backend must label user-uploaded data unverified.
    start=1_700_000_100 // 900 * 900
    rows=[]
    for i in range(count):
        opening=100.0 + i*.001
        close=opening + (.02 if i%4 else -.01)
        rows.append({"epoch":start+i*60,"open":opening,
                     "high":max(opening,close)+.05,
                     "low":min(opening,close)-.05,"close":close})
    return {"symbol":"TEST_OHLC","granularity_seconds":60,"candles":rows}


def _database(tmp_path,monkeypatch):
    engine=create_engine(f"sqlite:///{tmp_path}/research.sqlite")
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,"SessionLocal",factory)
    with factory() as session:
        a=db.User(deriv_account_id="V-OHLC-TEST-A")
        b=db.User(deriv_account_id="V-OHLC-TEST-B")
        session.add_all([a,b]);session.flush()
        for token,user in (("aa",a),("ab",a),("bb",b)):
            session.add(db.Session(
                session_hash=hashsecret("ohlc-"+token),
                user_id=user.id,token_encrypted=encrypt("fake-test-token"),
                expires_at=db.utcnow()+timedelta(days=2)))
        session.commit()
    return engine,factory


def test_reproducible_three_timeframes_and_unverified_provenance():
    input_data=sample()
    a=build_report(input_data)
    b=build_report(copy.deepcopy(input_data))
    assert a==b
    assert a["source"]=="USER_SUPPLIED_UNVERIFIED_OHLC"
    assert a["evidence"]["market_source_verified"] is False
    assert a["evidence"]["input_count"]==1140
    assert a["execution_allowed"] is False and a["orders_sent"]==0
    assert a["timeframe_alignment"]["approved_for_trading"] is False
    assert len(a["orchestrator"]["stages"])==8
    assert set(a["contexts"])=={"higher","intermediate","lower"}
    assert all(c["data_quality"]=="COMPLETE" for c in a["contexts"].values())
    assert a["period_comparison"]["method"]=="CHRONOLOGICAL_EQUAL_COUNT_HALVES"
    assert a["period_comparison"]["first"]["periods"]==570
    assert a["period_comparison"]["second"]["periods"]==570
    assert a["period_comparison"]["first"]["changes_observed"]==569
    assert "period_comparison" in summary(a)
    assert any(stage["module"]=="PERIOD_COMPARISON" for stage in a["orchestrator"]["stages"])
    assert a["statistics"]["changes_observed"]==1139
    assert a["statistics"]["interpretation"]=="DESCRIPTIVE_NOT_PREDICTIVE"
    assert len(a["evidence"]["input_sha256"])==64
    assert len(a["evidence"]["result_sha256"])==64
    assert "candles" not in summary(a)


@pytest.mark.parametrize("change",[
    lambda p: p["candles"][10].update(epoch=p["candles"][9]["epoch"]),
    lambda p: p["candles"][10].update(epoch=p["candles"][10]["epoch"]+60),
    lambda p: p["candles"][0].update(high=0.5),
    lambda p: p["candles"][0].update(close=float("nan")),
    lambda p: p["candles"][0].update(open=True),
    lambda p: p.update(granularity_seconds=300),
    lambda p: p.update(symbol="../bad"),
    lambda p: p["candles"].append(p["candles"][-1]),
])
def test_invalid_candles_fail_without_persistence(change):
    payload=sample()
    change(payload)
    with pytest.raises(ResearchInputError):
        build_report(payload)



@pytest.mark.parametrize("count", [1139, 1141, 1150, 1163])
def test_partial_final_timeframe_never_uses_unclosed_bucket(count):
    """Each role uses the last complete M1/M5/M15 close, never the open bar."""
    from app.intelligence2.historical_research import aggregate_closed_minutes
    payload=sample(count=count)
    report=build_report(payload)
    cutoff=report["period_to_epoch"]
    for role,seconds in (
        ("higher",900),("intermediate",300),("lower",60)
    ):
        sampled=aggregate_closed_minutes(payload["candles"],seconds)
        assert sampled,role
        assert sampled[-1]["epoch"]+seconds<=cutoff
        assert report["evidence"]["timeframe_asof_epochs"]["CONTEXT_"+role.upper()] == (
            sampled[-1]["epoch"] + seconds
        )
        assert report["contexts"][role]["observed_at_epoch"] == (
            sampled[-1]["epoch"] + seconds
        )
        assert all(item["epoch"]+seconds<=cutoff for item in sampled)
        assert report["contexts"][role]["state"]!="INDETERMINATE",role
        assert report["contexts"][role]["data_quality"]=="COMPLETE"
    assert report["execution_allowed"] is False
    assert report["source"]=="USER_SUPPLIED_UNVERIFIED_OHLC"


def test_less_than_72_complete_m15_candles_reports_insufficient_not_fabricated():
    data=build_report(sample(count=1068))
    assert data["contexts"]["higher"]["state"]=="INDETERMINATE"
    assert data["orchestrator"]["stages"][2]["quality"]=="INSUFFICIENT"
    assert data["contexts"]["lower"]["data_quality"]=="COMPLETE"
    assert data["timeframe_alignment"]["approved_for_trading"] is False


def test_rejects_not_closed_future_candles():
    payload=sample()
    with pytest.raises(ResearchInputError):
        build_report(payload,clock=payload["candles"][-1]["epoch"]+59)


def test_end_to_end_persists_and_isolates_without_ddl_or_order_writes(tmp_path,monkeypatch):
    engine,factory=_database(tmp_path,monkeypatch)
    statements=[]
    def track(conn,cursor,statement,params,ctx,many):
        statements.append(statement)
    event.listen(engine,"before_cursor_execute",track)
    try:
        unauth=TestClient(main.app)
        assert unauth.get("/api/nexora2/ohlc/research").status_code==401
        assert unauth.post("/api/nexora2/ohlc/research",json=sample()).status_code==401
        client=TestClient(main.app)
        client.cookies.set("nexora_sid","ohlc-aa")
        result=client.post("/api/nexora2/ohlc/research",json=sample())
        assert result.status_code==200,result.text
        report_id=result.json()["id"]
        assert result.json()["report"]["orders_sent"]==0
        assert len(client.get("/api/nexora2/ohlc/research").json()["items"])==1
        detail=client.get(f"/api/nexora2/ohlc/research/{report_id}")
        assert detail.status_code==200
        assert detail.json()["input"]["candles"]==sample()["candles"]
        assert client.post(f"/api/nexora2/ohlc/research/{report_id}/replay").json()["reproduced_identically"] is True
        other=TestClient(main.app)
        other.cookies.set("nexora_sid","ohlc-bb")
        assert other.get("/api/nexora2/ohlc/research").json()["items"]==[]
        assert other.get(f"/api/nexora2/ohlc/research/{report_id}").status_code==404
        assert other.post(f"/api/nexora2/ohlc/research/{report_id}/replay").status_code==404
        new_session=TestClient(main.app)
        new_session.cookies.set("nexora_sid","ohlc-ab")
        assert new_session.get(f"/api/nexora2/ohlc/research/{report_id}").status_code==200
        with factory() as session:
            stored=session.scalars(select(HistoricalResearchReport)).all()
            assert len(stored)==1
            assert "TEST_OHLC" in stored[0].payload_json
        assert not any(s.lstrip().upper().startswith(("CREATE","ALTER","DROP"))
                       for s in statements)
        assert not any("demo_orders" in s.lower() or "real_orders" in s.lower()
                       for s in statements)
    finally:
        event.remove(engine,"before_cursor_execute",track)
        engine.dispose()


def test_uploaded_research_bounded_and_invalid_payload_not_saved(tmp_path,monkeypatch):
    engine,factory=_database(tmp_path,monkeypatch)
    try:
        c=TestClient(main.app)
        c.cookies.set("nexora_sid","ohlc-aa")
        malformed=c.post("/api/nexora2/ohlc/research",json={"symbol":"BAD","candles":[]})
        assert malformed.status_code==422
        assert c.post("/api/nexora2/ohlc/research",
                      content=b"x"*280_001,
                      headers={"Content-Type":"application/json"}).status_code==413
        for n in range(MAX_PER_USER+2):
            payload=sample()
            payload["symbol"]=f"TEST_{n}"
            response=c.post("/api/nexora2/ohlc/research",json=payload)
            assert response.status_code==200,response.text
        assert len(c.get("/api/nexora2/ohlc/research").json()["items"])==MAX_PER_USER
        with factory() as s:
            assert s.query(HistoricalResearchReport).count()==MAX_PER_USER
    finally:
        engine.dispose()


def test_ui_contains_upload_and_replay_without_execution_routes():
    html=(Path(__file__).resolve().parents[1]/"app"/"nexora2.html").read_text()
    for key in ("ohlcFile","ohlcSymbol","ohlcRunButton","ohlcStudyOutput",
                "ohlcStages","ohlcPeriods","ohlcHistoryList","ohlcHistoryStatus",
                "ohlcStatus"):
        assert f'id="{key}"' in html
    assert "/api/nexora2/ohlc/research" in html
    assert "Reproduzir pesquisa" in html
    assert "dados enviados por você" in html
    for forbidden in ("/api/autopilot/start","/api/demo/buy",
                      "/api/real/buy","START_REAL_AUTOPILOT"):
        assert forbidden not in html


def test_uploaded_csv_parser_and_json_schema_in_node(tmp_path):
    """Verify actual browser CSV/JSON parser, without any broker or remote data."""
    import re
    import shutil
    import subprocess
    node=shutil.which("node")
    if not node:
        pytest.skip("Node.js not installed")
    html=(Path(__file__).resolve().parents[1]/"app"/"nexora2.html").read_text()
    match=re.search(r"<script>(.*?)</script>",html,flags=re.S)
    assert match
    script=match.group(1)
    assert script.rstrip().endswith("initNexora2();")
    script=script.replace("initNexora2();","")
    script += r"""
const csv='epoch,open,high,low,close\n1700000100,100,102,99,101\n';
const rows=parseUploadedOhlc(csv,'candles.csv');
if(rows.length!==1||rows[0].epoch!==1700000100||rows[0].close!==101)process.exit(31);
const json=JSON.stringify([{epoch:1700000100,open:100,high:102,low:99,close:101}]);
if(parseUploadedOhlc(json,'candles.json').length!==1)process.exit(32);
if(parseUploadedOhlc(JSON.stringify({candles:[{epoch:5}]}),'data.json').length!==1)
  process.exit(33);
let failed=false;
try{parseUploadedOhlc('timestamp,open,high,low,close\n1,1,2,1,2','bad.csv');}
catch{failed=true;}
if(!failed)process.exit(34);
"""
    filename=tmp_path/"ohlc-browser-parse.js"
    filename.write_text(script,encoding="utf-8")
    out=subprocess.run([node,str(filename)],text=True,capture_output=True,timeout=15)
    assert out.returncode==0,out.stderr


def test_tampered_saved_evidence_rejected_on_detail_and_replay(tmp_path, monkeypatch):
    engine,factory=_database(tmp_path,monkeypatch)
    try:
        client=TestClient(main.app)
        client.cookies.set("nexora_sid","ohlc-aa")
        created=client.post("/api/nexora2/ohlc/research",json=sample())
        assert created.status_code==200,created.text
        item_id=created.json()["id"]
        with factory() as session:
            row=session.get(HistoricalResearchReport,item_id)
            data=json.loads(row.payload_json)
            data["input"]["candles"][0]["close"]+=0.01
            row.payload_json=json.dumps(data,allow_nan=False)
            session.commit()
        detail=client.get(f"/api/nexora2/ohlc/research/{item_id}")
        assert detail.status_code==409
        assert "Integridade" in detail.json()["detail"]
        replay=client.post(f"/api/nexora2/ohlc/research/{item_id}/replay")
        assert replay.status_code==409
        with factory() as session:
            assert session.query(HistoricalResearchReport).count()==1
    finally:
        engine.dispose()


def test_corrupted_stored_ohlc_metadata_is_rejected(tmp_path, monkeypatch):
    engine,factory=_database(tmp_path,monkeypatch)
    try:
        client=TestClient(main.app)
        client.cookies.set("nexora_sid","ohlc-aa")
        created=client.post("/api/nexora2/ohlc/research",json=sample())
        assert created.status_code==200
        item_id=created.json()["id"]
        with factory() as session:
            row=session.get(HistoricalResearchReport,item_id)
            data=json.loads(row.payload_json)
            data["orders_sent"]=3
            row.payload_json=json.dumps(data,allow_nan=False)
            session.commit()
        assert client.get(f"/api/nexora2/ohlc/research/{item_id}").status_code==409
    finally:
        engine.dispose()


def test_partial_odd_count_period_comparison_is_chronological():
    report=build_report(sample(count=1139))
    first=report["period_comparison"]["first"]
    second=report["period_comparison"]["second"]
    assert first["periods"]==569
    assert second["periods"]==570
    assert first["changes_observed"]==568
    assert second["changes_observed"]==569
    assert report["period_comparison"]["split_epoch"]==sample(1139)["candles"][569]["epoch"]
    assert report["period_comparison"]["interpretation"]=="DESCRIPTIVE_NOT_PREDICTIVE"
    assert report["orders_sent"]==0


@pytest.mark.parametrize("invalid_clock",[
    0,-1,True,False,1.5,float("nan"),float("inf"),"2026-11-21",
])
def test_invalid_replay_clock_is_rejected_explicitly(invalid_clock):
    with pytest.raises(ResearchInputError,match="Relógio inválido"):
        build_report(sample(),clock=invalid_clock)


def test_tampered_statistical_result_is_rejected_even_with_intact_input(
        tmp_path,monkeypatch):
    engine,factory=_database(tmp_path,monkeypatch)
    try:
        client=TestClient(main.app)
        client.cookies.set("nexora_sid","ohlc-aa")
        created=client.post("/api/nexora2/ohlc/research",json=sample())
        assert created.status_code==200,created.text
        report_id=created.json()["id"]
        with factory() as session:
            record=session.get(HistoricalResearchReport,report_id)
            body=json.loads(record.payload_json)
            untouched_input_hash=body["evidence"]["input_sha256"]
            body["statistics"]["up_intervals"]+=1
            assert body["evidence"]["input_sha256"]==untouched_input_hash
            record.payload_json=json.dumps(body,allow_nan=False)
            session.commit()
        details=client.get(f"/api/nexora2/ohlc/research/{report_id}")
        assert details.status_code==409
        assert "Resultado" in details.json()["detail"]
        replay=client.post(f"/api/nexora2/ohlc/research/{report_id}/replay")
        assert replay.status_code==409
    finally:
        engine.dispose()

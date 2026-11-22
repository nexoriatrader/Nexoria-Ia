"""User-owned historical research comparison regression tests (no orders)."""
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db, main
from app.intelligence2.historical_research import build_report
from app.intelligence2.research_compare import compare_saved_reports
from app.security import encrypt, hashsecret


def _sample(symbol="SAMPLE_A", delta=.003, count=1140):
    first=1_700_000_100 // 900 * 900
    bars=[]
    for n in range(count):
        value=100.0+n*delta
        last=value+(.025 if n%3 else -.015)
        bars.append({"epoch":first+60*n,"open":value,
                     "high":max(value,last)+.05,
                     "low":min(value,last)-.05,"close":last})
    return {"symbol":symbol,"granularity_seconds":60,"candles":bars}


def test_pure_comparison_reproducible_descriptive_not_ranking():
    left=build_report(_sample())
    right=build_report(_sample(symbol="SAMPLE_B",delta=.006))
    result=compare_saved_reports(left,right)
    assert result==compare_saved_reports(left,right)
    assert result["comparison_kind"]=="CROSS_ASSET"
    assert result["source"]=="USER_SUPPLIED_UNVERIFIED_OHLC"
    assert result["execution_allowed"] is False
    assert result["orders_sent"]==0
    assert result["left"]["observations"]==1139
    assert result["right"]["observations"]==1139
    assert result["left"]["input_sha256"]!=result["right"]["input_sha256"]
    assert result["matching_windows"] is True
    for field in ("up_fraction","down_fraction","unchanged_fraction"):
        assert 0<=result["left"][field]<=1
        assert 0<=result["right"][field]<=1
    assert "ranking" in result["disclaimer"].lower()
    assert "profit" not in result
    assert "best_asset" not in result


def test_authenticated_compare_cannot_read_another_users_study(tmp_path,monkeypatch):
    engine=create_engine(f"sqlite:///{tmp_path}/compared.sqlite")
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,"SessionLocal",factory)
    try:
        with factory() as session:
            a=db.User(deriv_account_id="V-COMPARE-A")
            b=db.User(deriv_account_id="V-COMPARE-B")
            session.add_all([a,b]);session.flush()
            for name,user in (("a",a),("b",b)):
                session.add(db.Session(
                    session_hash=hashsecret("compare-"+name),
                    user_id=user.id,
                    token_encrypted=encrypt("test-token"),
                    expires_at=db.utcnow()+timedelta(days=1)))
            session.commit()
        a=TestClient(main.app)
        b=TestClient(main.app)
        a.cookies.set("nexora_sid","compare-a")
        b.cookies.set("nexora_sid","compare-b")
        path="/api/nexora2/ohlc/research"
        assert TestClient(main.app).get(path+"/compare?left_id=1&right_id=2").status_code==401
        one=a.post(path,json=_sample("AA_1"))
        two=a.post(path,json=_sample("AA_2",delta=.009))
        three=b.post(path,json=_sample("BB_1",delta=.014))
        assert all(x.status_code==200 for x in (one,two,three))
        left,right,foreign=(x.json()["id"] for x in (one,two,three))
        compare=a.get(path+f"/compare?left_id={left}&right_id={right}")
        assert compare.status_code==200,compare.text
        body=compare.json()
        assert body["ids"]=={"left":left,"right":right}
        assert body["source"]=="USER_SUPPLIED_UNVERIFIED_OHLC"
        assert body["execution_allowed"] is False
        assert body["orders_sent"]==0
        assert a.get(path+f"/compare?left_id={left}&right_id={foreign}").status_code==404
        assert b.get(path+f"/compare?left_id={left}&right_id={foreign}").status_code==404
        assert a.get(path+f"/compare?left_id={left}&right_id={left}").status_code==422
        assert a.get(path+"/compare?left_id=0&right_id=1").status_code==422
        assert a.get(path+"/compare?left_id=no&right_id=2").status_code==422
    finally:
        engine.dispose()


def test_compare_interface_not_broker_execution():
    from pathlib import Path
    html=(Path(__file__).resolve().parents[1]/"app"/"nexora2.html").read_text()
    for name in ("ohlcCompareLeft","ohlcCompareRight","ohlcCompareButton",
                 "ohlcCompareResult","ohlcCompareStatus"):
        assert f'id="{name}"' in html
    assert "compareUploadedOhlcReports()" in html
    assert "api/nexora2/ohlc/research/compare?left_id=" in html
    assert "não verificadas; sem previsão ou ranking" in html
    for forbidden in ("/api/autopilot/start","/api/real/buy","/api/demo/buy"):
        assert forbidden not in html

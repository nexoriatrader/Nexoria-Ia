"""Technical-only synthetic homologation: test ingestion, auth, restart."""
from datetime import datetime, timezone
import os
import time

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from app.intelligence2 import stage4_general_homologation as demo


def setup_test(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "stage4_test.db"))
    demo.meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(demo.insert(demo.revisions).values(
            revision=demo.SCHEMA_REVISION, applied_at=datetime.now(timezone.utc)))
        conn.execute(demo.insert(demo.revisions).values(
            revision=demo.observer.REVISION, applied_at=datetime.now(timezone.utc)))
        conn.execute(demo.insert(demo.revisions).values(
            revision=demo.pipeline.REVISION, applied_at=datetime.now(timezone.utc)))
    monkeypatch.setattr(demo, "_engine", lambda: engine)
    monkeypatch.setenv("NEXORA_STAGE4_ACCESS_TOKEN", "test-only-strong-secret-32-characters-long")
    return engine


def test_production_and_bad_db_fail_closed(monkeypatch):
    monkeypatch.delenv("NEXORA_STAGE4_GENERAL_SYNTHETIC_ONLY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://root:pw@prod.example/db")
    try:
        demo.require_isolated_postgres()
        assert False
    except RuntimeError:
        pass
    monkeypatch.setenv("NEXORA_STAGE4_GENERAL_SYNTHETIC_ONLY", "true")
    try:
        demo.require_isolated_postgres()
        assert False
    except RuntimeError:
        pass


def test_classification_is_never_financial():
    for _, metric, _ in demo.STATIONS:
        for value in (10, 30, 500):
            assert demo._classify(metric, value) in (
                "NORMAL", "REVIEW", "OUTSIDE_RANGE")


def test_idempotent_persistence_and_history(monkeypatch, tmp_path):
    engine = setup_test(monkeypatch, tmp_path)
    at = 1795194000
    first = demo.ingest_batch(now_epoch=at, interval=90)
    second = demo.ingest_batch(now_epoch=at, interval=90)
    assert first["inserted"] == len(demo.STATIONS)
    assert second["inserted"] == 0
    assert demo.read_status()["records"] == len(demo.STATIONS)
    assert all(row["source"] == demo.SOURCE for row in demo.read_history())
    engine.dispose()
    # A fresh SQLAlchemy engine simulates a process restart.
    restarted = create_engine("sqlite:///" + str(tmp_path / "stage4_test.db"))
    monkeypatch.setattr(demo, "_engine", lambda: restarted)
    assert demo.read_status()["records"] == len(demo.STATIONS)
    assert demo.ingest_batch(now_epoch=at + 180, interval=90)["inserted"] == 3
    assert demo.read_status()["records"] == 6
    restarted.dispose()


def test_http_auth_panel_health_and_history(monkeypatch, tmp_path):
    engine = setup_test(monkeypatch, tmp_path)
    demo.ingest_batch(now_epoch=1795194000, interval=90)
    client = TestClient(demo.app)
    assert client.get("/").status_code == 200
    assert "DADOS SINTÉTICOS" in client.get("/").text
    assert client.get("/healthz").json()["records"] == 3
    assert client.get("/api/homologation/demo/history").status_code == 401
    assert client.get("/api/homologation/demo/status",
                     headers={"X-NEXORA-STAGE4-KEY": "bad"}).status_code == 401
    headers = {"X-NEXORA-STAGE4-KEY":
               "test-only-strong-secret-32-characters-long"}
    status = client.get("/api/homologation/demo/status", headers=headers)
    assert status.status_code == 200 and status.json()["synthetic"] is True
    history = client.get("/api/homologation/demo/history?limit=2",
                         headers=headers)
    assert history.status_code == 200 and history.json()["count"] == 2
    assert all(x["source"] == demo.SOURCE for x in history.json()["rows"])
    assert client.get("/api/homologation/demo/history?limit=101",
                      headers=headers).status_code == 422
    assert "/api/demo/buy" not in {r.path for r in demo.app.routes}
    assert "/api/real/orders" not in {r.path for r in demo.app.routes}
    assert client.get("/api/homologation/monitor/overview").status_code == 401
    overview = client.get("/api/homologation/monitor/overview", headers=headers)
    assert overview.status_code == 200
    o = overview.json()
    assert o["summary"]["stored_records"] == 3
    assert o["summary"]["total_runs"] == 1
    assert o["summary"]["new_insertions_observed_since_monitoring"] == 3
    assert o["synthetic"] is True
    assert o["health"]["integrity_sample"]["status"] == "PASS"
    for module in o["modules"]:
        if module["id"].startswith("I") or module["id"] == "ORCH":
            assert module["running_in_service"] is False
            assert module["last_update"] is None
            assert module["status"] in ("NOT_CONNECTED", "NOT_AVAILABLE")
    runs = client.get("/api/homologation/monitor/runs", headers=headers)
    assert runs.status_code == 200 and runs.json()["count"] == 1
    assert client.get("/api/homologation/monitor/modules", headers=headers).status_code == 200
    assert client.get("/api/homologation/monitor/diagnostics", headers=headers).status_code == 200
    engine.dispose()


def test_monitoring_duplicate_audit_restart_and_invalid_counts(monkeypatch, tmp_path):
    engine = setup_test(monkeypatch, tmp_path)
    demo.ingest_batch(now_epoch=1795194000, interval=90)
    duplicate = demo.ingest_batch(now_epoch=1795194000, interval=90)
    assert duplicate["inserted"] == 0
    o = demo.observer.get_overview()
    assert o["summary"]["stored_records"] == 3
    assert o["summary"]["total_runs"] == 2
    assert o["summary"]["processed_observations"] == 6
    assert o["summary"]["duplicate_observations"] == 3
    assert o["summary"]["invalid_observations"] == 0
    assert o["summary"]["incomplete_observations"] == 0
    assert any(x["code"] == "DUPLICATE_EVENT_KEY" for x in o["diagnostics"])
    engine.dispose()
    restarted = create_engine("sqlite:///" + str(tmp_path / "stage4_test.db"))
    monkeypatch.setattr(demo, "_engine", lambda: restarted)
    assert demo.observer.get_overview()["summary"]["total_runs"] == 2
    assert demo.observer.get_overview()["summary"]["stored_records"] == 3
    restarted.dispose()


def test_pipeline_runs_actual_evidence_lifecycle_helper_and_is_persisted(monkeypatch,tmp_path):
    engine=setup_test(monkeypatch,tmp_path)
    first=demo.ingest_batch(now_epoch=1795194000,interval=90)
    again=demo.ingest_batch(now_epoch=1795194000,interval=90)
    assert first["inserted"]==3 and again["inserted"]==0
    trace=demo.pipeline.recent(limit=20)
    assert len(trace)==10
    assert set(item["stage"] for item in trace)==set(demo.pipeline.STAGES)
    lifecycle=[x for x in trace if x["stage"]=="I06_EVIDENCE_LIFECYCLE_UTILITY"]
    assert len(lifecycle)==2
    assert lifecycle[0]["output"]["outcome"]["lifecycle"]=="UNCHANGED"
    assert lifecycle[1]["output"]["outcome"]["lifecycle"]=="NEW"
    summary=demo.pipeline.summary()
    assert summary["stage_executions"]==10
    assert summary["cycles_observed"]==1
    engine.dispose()


def test_pipeline_api_auth_and_nonfinancial_module_status(monkeypatch,tmp_path):
    engine=setup_test(monkeypatch,tmp_path)
    demo.ingest_batch(now_epoch=1795194000, interval=90)
    client=TestClient(demo.app)
    assert client.get("/api/homologation/monitor/pipeline").status_code==401
    headers={"X-NEXORA-STAGE4-KEY":"test-only-strong-secret-32-characters-long"}
    r=client.get("/api/homologation/monitor/pipeline?limit=10",headers=headers)
    assert r.status_code==200
    assert r.json()["summary"]["stage_executions"]==5
    assert r.json()["full_I01_to_I06_integrated"] is False
    assert client.get("/api/homologation/monitor/pipeline?limit=101",headers=headers).status_code==422
    status=client.get("/api/homologation/monitor/modules",headers=headers).json()
    assert all(x["running_in_service"] is False for x in status["modules"]
               if x["id"] in ("I01","I02","I03","I04","I05","I06","ORCH"))
    engine.dispose()


def test_invalid_synthetic_source_fails_closed_and_creates_no_records(monkeypatch,tmp_path):
    engine=setup_test(monkeypatch,tmp_path)
    monkeypatch.setattr(demo,"_value",lambda *args: float("nan"))
    import pytest
    with pytest.raises(ValueError,match="Invalid synthetic sensor"):
        demo.ingest_batch(now_epoch=1795194000,interval=90)
    assert demo.read_status()["records"]==0
    assert demo.pipeline.summary()["stage_executions"]==0
    engine.dispose()


def test_multi_cycle_and_restart_preserve_stage_outputs(monkeypatch,tmp_path):
    engine=setup_test(monkeypatch,tmp_path)
    demo.ingest_batch(now_epoch=1795194000,interval=90)
    demo.ingest_batch(now_epoch=1795194180,interval=90)
    assert demo.pipeline.summary()["cycles_observed"]==2
    assert demo.pipeline.summary()["stage_executions"]==10
    engine.dispose()
    restarted=create_engine("sqlite:///"+str(tmp_path/"stage4_test.db"))
    monkeypatch.setattr(demo,"_engine",lambda:restarted)
    assert demo.pipeline.summary()["cycles_observed"]==2
    assert demo.read_status()["records"]==6
    restarted.dispose()

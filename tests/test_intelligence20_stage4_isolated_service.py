"""Isolated Stage4 routing, safety and loop tests; never call Deriv."""
import asyncio
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from app import db
from app.intelligence2 import stage4_homologation_service as stage


def _env(monkeypatch):
    values = {
        "NEXORA_DEPLOYMENT_STAGE": "homologation",
        "NEXORA_INTELLIGENCE_HOMOLOGATION_READONLY_ENABLED": "true",
        "NEXORA_INTELLIGENCE_DISTRIBUTED_RESEARCH_ENABLED": "true",
        "NEXORA_INTELLIGENCE_HOMOLOGATION_LIVE_WORKER_ENABLED": "true",
        "NEXORA_REAL_AUTOPILOT_ENABLED": "false",
        "NEXORA_REAL_RELEASE_APPROVED": "false",
        "NEXORA_DEMO_BUY_ENABLED": "false",
        "NEXORA_HOMOLOGATION_DATABASE_NAME": "research_homologation",
        "NEXORA_HOMOLOGATION_DATABASE_HOST": "isolated-db.example.org",
        "NEXORA_HOMOLOGATION_ACCESS_TOKEN":
            "local-test-key-not-for-deployment-32-characters",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(db, "DB_URL",
        "postgresql+psycopg://test:test@isolated-db.example.org/research_homologation")


def test_stage_and_db_guard_fails_closed(monkeypatch):
    _env(monkeypatch)
    stage.isolated_database_guard()
    monkeypatch.setenv("NEXORA_DEPLOYMENT_STAGE", "production")
    with pytest.raises(RuntimeError):
        stage.isolated_database_guard()
    monkeypatch.setenv("NEXORA_DEPLOYMENT_STAGE", "homologation")
    monkeypatch.setenv("NEXORA_HOMOLOGATION_DATABASE_HOST", "production-db.example.org")
    with pytest.raises(RuntimeError):
        stage.isolated_database_guard()
    monkeypatch.setenv("NEXORA_HOMOLOGATION_DATABASE_HOST", "isolated-db.example.org")
    monkeypatch.setenv("NEXORA_DEMO_BUY_ENABLED", "true")
    with pytest.raises(RuntimeError):
        stage.isolated_database_guard()


def test_no_financial_routes_present():
    paths = {route.path for route in stage.app.routes}
    assert "/api/homologation/robot/research" in paths
    assert "/api/homologation/status" in paths
    assert "/api/demo/buy" not in paths
    assert "/api/autopilot/start" not in paths
    assert "/api/real/orders" not in paths


def test_authenticated_read_only_shared_display(monkeypatch):
    _env(monkeypatch)
    views = []
    def fake(*, view):
        views.append(view)
        if view == "unknown":
            raise ValueError()
        return {"state": "CURRENT_PUBLIC_RESEARCH",
                "items": [], "snapshot_digest": "a" * 64,
                "execution_allowed": False, "orders_sent": 0}
    monkeypatch.setattr(stage, "robot_research_view", fake)
    client = TestClient(stage.app)  # no lifespan, so no live worker
    headers = {"X-NEXORA-HOMOLOGATION-KEY":
               "local-test-key-not-for-deployment-32-characters"}
    assert client.get("/api/homologation/status").status_code == 401
    assert client.get("/api/homologation/status",
        headers={"X-NEXORA-HOMOLOGATION-KEY": "invalid"}).status_code == 401
    for view in ("autonomous", "signals", "advanced"):
        response = client.get(
            "/api/homologation/robot/research?view=" + view, headers=headers)
        assert response.status_code == 200
        assert response.json()["orders_sent"] == 0
    assert client.get("/api/homologation/robot/research?view=unknown",
                      headers=headers).status_code == 422
    status = client.get("/api/homologation/status", headers=headers)
    assert status.status_code == 200
    assert status.json()["execution_allowed"] is False
    assert status.json()["real_feed_confirmed"] is False
    assert status.headers["cache-control"] == "no-store"
    assert views == ["autonomous", "signals", "advanced", "unknown", "autonomous"]


def test_idempotent_per_closed_m1_candle():
    now = datetime(2026, 11, 20, 17, 4, 30, tzinfo=timezone.utc)
    identity, metadata = stage._closed_candle_identity(now)
    assert metadata["closed_epoch"] == (int(now.timestamp()) // 60 - 1) * 60
    assert (identity, metadata) == stage._closed_candle_identity(
        now.replace(second=40))


def test_bounded_live_loop_uses_only_public_pipeline(monkeypatch):
    _env(monkeypatch)
    seen = []
    async def one_turn():
        stop = asyncio.Event()
        async def fake(**kwargs):
            seen.append(kwargs)
            stop.set()
            return {"enqueue": {"created": True}, "worker_state": "RESEARCH_SAVED",
                    "execution_allowed": False, "orders_sent": 0}
        monkeypatch.setattr(stage, "run_homologation_cycle", fake)
        await stage.run_live_loop(stop, interval=90)
    asyncio.run(one_turn())
    assert len(seen) == 1
    assert seen[0]["symbol"] == "SHARED"
    assert seen[0]["market"] == "public"
    assert stage._last_cycle["state"] == "RESEARCH_SAVED"


def test_bootstrap_never_runs_without_confirmation():
    from scripts.bootstrap_intelligence20_homologation import initialize
    with pytest.raises(RuntimeError, match="confirmation"):
        initialize()

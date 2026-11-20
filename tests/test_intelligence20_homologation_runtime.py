"""Read-only robot homologation: real API security and public DTO contract.

Unit tests use no finance engine, broker, production database or orders.
Persistent end-to-end execution is covered separately on disposable PG.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main
from app.intelligence2 import homologation_runtime as runtime

NOW = 1_800_000_020


def _enable(monkeypatch):
    monkeypatch.setenv("NEXORA_DEPLOYMENT_STAGE", "homologation")
    monkeypatch.setenv("NEXORA_INTELLIGENCE_HOMOLOGATION_READONLY_ENABLED", "true")
    monkeypatch.setenv("NEXORA_INTELLIGENCE_DISTRIBUTED_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("NEXORA_REAL_AUTOPILOT_ENABLED", "false")
    monkeypatch.setenv("NEXORA_REAL_RELEASE_APPROVED", "false")


def _safe_item():
    return {
        "market": "forex", "symbol": "FRXEURUSD",
        "state": "RESEARCH_READY",
        "observed_public_trend": "UP",
        "evidence_completeness_points": 100,
        "metric_kind": "DATA_COMPLETENESS_NOT_PROBABILITY",
        "fingerprint": "a" * 64,
        "valid_until_epoch": NOW+60,
        "reasons": ["PUBLIC_DATA_READY_FOR_STUDY"],
        "knowledge_status": "NOT_CONNECTED",
        "learning_status": "NOT_CONNECTED",
        "execution_allowed": False,
    }


@pytest.mark.parametrize("stage,flag,distributed,real,approved,expected", [
    ("homologation", "true", "true", "false", "false", True),
    ("production", "true", "true", "false", "false", False),
    ("homologation", "false", "true", "false", "false", False),
    ("homologation", "true", "false", "false", "false", False),
    ("homologation", "true", "true", "true", "false", False),
    ("homologation", "true", "true", "false", "true", False),
])
def test_three_nonfinancial_homologation_gates(
    monkeypatch, stage, flag, distributed, real, approved, expected,
):
    for name, value in (
        ("NEXORA_DEPLOYMENT_STAGE", stage),
        ("NEXORA_INTELLIGENCE_HOMOLOGATION_READONLY_ENABLED", flag),
        ("NEXORA_INTELLIGENCE_DISTRIBUTED_RESEARCH_ENABLED", distributed),
        ("NEXORA_REAL_AUTOPILOT_ENABLED", real),
        ("NEXORA_REAL_RELEASE_APPROVED", approved),
    ):
        monkeypatch.setenv(name, value)
    assert runtime.homologation_enabled() is expected


@pytest.mark.parametrize("mutate", [
    lambda row: row.update(user_id=42),
    lambda row: row.update(trade_side="CALL"),
    lambda row: row.update(entry_epoch=NOW),
    lambda row: row.update(evidence_completeness_points=95),
    lambda row: row.update(valid_until_epoch=NOW),
    lambda row: row.update(metric_kind="WIN_PROBABILITY"),
    lambda row: row.update(execution_allowed=True),
    lambda row: row.update(fingerprint="not-a-digest"),
])
def test_untrusted_or_expired_public_item_is_not_exposed(mutate):
    item = _safe_item()
    mutate(item)
    assert runtime._safe_public_item(item, now_epoch=NOW) is None


def test_public_result_is_minimal_and_nonoperational():
    row = runtime._safe_public_item(_safe_item(), now_epoch=NOW)
    assert row["review_state"] == "RESEARCH_READY"
    assert row["evidence_completeness_points"] == 100
    assert row["valid_until_epoch"] == NOW+60
    assert "observed_public_trend" not in row
    assert "trade_side" not in row
    assert "entry_epoch" not in row
    assert "contract_expiry_minutes" not in row
    assert "account_id" not in row
    assert "stake" not in row


@pytest.mark.parametrize("view", ["autonomous", "signals", "advanced"])
def test_robot_views_are_shared_and_cannot_change_orders(
    monkeypatch, view,
):
    _enable(monkeypatch)
    observed = []
    def fake_shared(**kwargs):
        observed.append(kwargs)
        return {
            "state": "CURRENT_PUBLIC_RESEARCH",
            "items": [{"symbol": "FRXEURUSD", "review_state": "WAIT_RECHECK"}],
            "snapshot_digest": "a"*64,
        }
    monkeypatch.setattr(runtime, "load_shared_public_snapshot", fake_shared)
    result = runtime.robot_research_view(view=view, now_epoch=NOW)
    assert result["items"] == [{"symbol": "FRXEURUSD", "review_state": "WAIT_RECHECK"}]
    assert result["read_only"] is True
    assert result["shared_intelligence"] is True
    assert result["orders_sent"] == 0
    assert result["execution_allowed"] is False
    assert result["modifies_robot_settings"] is False
    assert result["report_kind"] == "PUBLIC_RESEARCH_CONTEXT_NOT_TRADING_SIGNALS"
    assert observed == [{"now_epoch": NOW}]


def test_robot_get_is_authenticated_off_by_default_and_no_scans(monkeypatch):
    monkeypatch.setattr(main, "active_user", lambda request, sid: SimpleNamespace(id=1))
    getter = Mock(side_effect=AssertionError("must not query DB when disabled"))
    monkeypatch.setattr(runtime, "load_shared_public_snapshot", getter)
    monkeypatch.delenv("NEXORA_INTELLIGENCE_HOMOLOGATION_READONLY_ENABLED", raising=False)
    client = TestClient(main.app)
    try:
        result = client.get("/api/homologation/robot/research")
        assert result.status_code == 503
    finally:
        client.close()
    getter.assert_not_called()


def test_robot_authentication_before_shared_snapshot_read(monkeypatch):
    _enable(monkeypatch)
    def unauthorized(*args):
        raise HTTPException(401, "Unauthorized")
    monkeypatch.setattr(main, "active_user", unauthorized)
    getter = Mock(side_effect=AssertionError("not authenticated"))
    monkeypatch.setattr(runtime, "load_shared_public_snapshot", getter)
    client = TestClient(main.app)
    try:
        assert client.get("/api/homologation/robot/research").status_code == 401
    finally:
        client.close()
    getter.assert_not_called()


def test_robot_route_returns_only_shared_nonfinancial_research(monkeypatch):
    _enable(monkeypatch)
    users = [SimpleNamespace(id=17), SimpleNamespace(id=98)]
    monkeypatch.setattr(main, "active_user", lambda *args: users.pop(0))
    getter = Mock(return_value={
        "state": "CURRENT_PUBLIC_RESEARCH",
        "items": [{"market": "forex", "symbol": "FRXEURUSD",
                   "review_state": "WAIT_RECHECK"}],
        "snapshot_digest": "b"*64,
    })
    monkeypatch.setattr(runtime, "load_shared_public_snapshot", getter)
    client = TestClient(main.app)
    try:
        one = client.get("/api/homologation/robot/research?view=autonomous")
        two = client.get("/api/homologation/robot/research?view=advanced")
    finally:
        client.close()
    assert one.status_code == 200 and two.status_code == 200
    assert one.json()["items"] == two.json()["items"]
    assert one.json()["snapshot_digest"] == two.json()["snapshot_digest"]
    assert one.json()["execution_allowed"] is False
    assert one.json()["orders_sent"] == 0
    assert "user_id" not in str(one.json())
    assert "stake" not in str(one.json())
    assert getter.call_count == 2


def test_invalid_view_is_rejected_and_research_is_not_mutated(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(main, "active_user", lambda *args: SimpleNamespace(id=1))
    getter = Mock(side_effect=AssertionError("invalid view must not read"))
    monkeypatch.setattr(runtime, "load_shared_public_snapshot", getter)
    client = TestClient(main.app)
    try:
        response = client.get("/api/homologation/robot/research?view=execute")
    finally:
        client.close()
    assert response.status_code == 422
    getter.assert_not_called()


def test_manual_cycle_gate_prevents_queue_access_when_disabled(monkeypatch):
    monkeypatch.setenv("NEXORA_DEPLOYMENT_STAGE", "production")
    queued = Mock(side_effect=AssertionError("cannot enqueue"))
    worker = AsyncMock(side_effect=AssertionError("cannot run"))
    monkeypatch.setattr(runtime, "enqueue_public_task", queued)
    monkeypatch.setattr(runtime, "run_one_public_research_task", worker)
    with pytest.raises(PermissionError):
        asyncio.run(runtime.run_homologation_cycle(
            task_key="research_homologation_0001", market="forex",
            symbol="FRXEURUSD",
            source={"source_version": "i03_public_v1",
                    "closed_epoch": NOW-60, "timeframe_seconds": 60},
            worker_id="public_worker_1",
        ))
    queued.assert_not_called()
    worker.assert_not_awaited()

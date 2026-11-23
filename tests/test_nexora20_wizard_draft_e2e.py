"""Isolated end-to-end demo-planning tests. Never start the app lifespan or a broker."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import db, main
from app.security import encrypt, hashsecret


def planning_client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/wizard-draft.sqlite")
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    with factory() as sess:
        for name, cookie in (("VDRAFT_A", "draft_a"), ("VDRAFT_B", "draft_b")):
            user = db.User(deriv_account_id=name, selected_mode="demo")
            sess.add(user)
            sess.flush()
            sess.add(db.Session(
                session_hash=hashsecret(cookie), user_id=user.id,
                token_encrypted=encrypt("fake-test-token"),
                expires_at=db.utcnow() + timedelta(hours=1),
            ))
            sess.add(db.AutomationProfile(
                user_id=user.id, account_mode="demo",
                market="derived", symbols_json='["1HZ100V"]',
            ))
        sess.commit()
        user_ids = [item.id for item in sess.query(db.User).order_by(db.User.id)]
    client = TestClient(main.app)
    client.cookies.set("nexora_sid", "draft_a")
    return client, factory, engine, user_ids


BASE = {
    "mode": "manual", "stake": "0.50", "stop_win": "5.00",
    "stop_loss": "3.00", "recovery": "cycles", "cycles": 2,
    "multiplier": "1.50", "martingale_attempts": 0,
}


def test_atomic_wizard_draft_is_per_user_and_does_not_apply_to_engine(tmp_path, monkeypatch):
    client, factory, engine, (a, b) = planning_client(tmp_path, monkeypatch)
    try:
        before = client.get("/api/robot/wizard")
        assert before.status_code == 200
        assert before.json()["saved"] is False
        response = client.post("/api/robot/wizard", json=BASE)
        assert response.status_code == 200, response.text
        assert response.json()["starts_robot"] is False
        assert response.json()["applied_to_executor"] is False
        assert response.json()["mode"] == "manual"
        assert client.post('/api/autopilot/start', json={'confirm': 'START_DEMO_AUTOPILOT'}).status_code == 409
        again = client.get("/api/robot/wizard").json()
        assert again["saved"] is True
        assert again["draft"]["cycles"] == 2
        # A separate browser client reads the identical durable preference record.
        later_client = TestClient(main.app)
        later_client.cookies.set("nexora_sid", "draft_a")
        recovered = later_client.get("/api/robot/wizard").json()
        assert recovered["saved"] is True
        assert recovered["draft"] == again["draft"]
        later_client.close()
        assert again["draft"]["multiplier"] == "1.50"
        with factory() as sess:
            assert sess.get(db.User, a).stake == "0.50"
            assert sess.get(db.User, a).stop_loss == "0.00"
            assert sess.get(db.AutomationProfile, a).account_mode == "demo"
            assert sess.get(db.RobotPlan, a) is None
            assert sess.get(db.RobotUsageMode, b) is None
        client.cookies.set("nexora_sid", "draft_b")
        assert client.get("/api/robot/wizard").json()["saved"] is False
        assert client.get("/api/robot/wizard").json()["mode"] == "intelligent"
    finally:
        engine.dispose()


def test_invalid_drafts_leave_valid_record_unchanged(tmp_path, monkeypatch):
    client, factory, engine, (a, _) = planning_client(tmp_path, monkeypatch)
    try:
        assert client.post("/api/robot/wizard", json=BASE).status_code == 200
        cases = [
            {"recovery": "martingale", "cycles": 2, "martingale_attempts": 1},
            {"mode": "intelligent", "recovery": "martingale", "cycles": 0,
             "martingale_attempts": 1},
            {"recovery": "none", "cycles": 2},
            {"recovery": "cycles", "cycles": 0},
            {"stake": "nan"},
            {"stake": "0.10"},
            {"stop_loss": "-2"},
            {"multiplier": "0.5"},
            {"multiplier": "101"},
            {"multiplier": "2.1234"},
            {"mode": "unknown"},
        ]
        for diff in cases:
            response = client.post("/api/robot/wizard", json={**BASE, **diff})
            assert response.status_code == 422, (diff, response.text)
        after = client.get("/api/robot/wizard").json()
        assert after["draft"]["cycles"] == 2
        assert after["mode"] == "manual"
        main.autopilot_enabled.add(a)
        try:
            assert client.post("/api/robot/wizard", json=BASE).status_code == 409
        finally:
            main.autopilot_enabled.discard(a)
    finally:
        engine.dispose()


def test_manual_schedule_is_private_and_elapsed_is_never_executed(tmp_path, monkeypatch):
    client, factory, engine, (a, b) = planning_client(tmp_path, monkeypatch)
    # Everything here is a saved annotation, not a Deriv quote or purchase.
    fake_catalogue = AsyncMock(return_value=[
        {"symbol": "1HZ100V", "name": "Sample labelled catalogue item"}
    ])
    monkeypatch.setattr(main, "featured_instruments_by_group", fake_catalogue)
    buy = AsyncMock(side_effect=AssertionError("Broker execution is forbidden in planning"))
    monkeypatch.setattr(main, "demo_buy", buy)
    when = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
    try:
        # A new manual-only user should not need to set up the automatic executor.
        with factory() as sess:
            sess.delete(sess.get(db.AutomationProfile, a))
            sess.commit()
        assert client.post("/api/robot/wizard", json=BASE).status_code == 200
        post = client.post("/api/robot/manual-signals", json={
            "symbol": "1HZ100V", "direction": "CALL",
            "expiry_minutes": 5, "scheduled_for": when,
        })
        assert post.status_code == 200, post.text
        signal_id = post.json()["signal"]["id"]
        assert post.json()["execution"] == "SCHEDULE_ONLY_NO_ORDERS"
        duplicate = client.post("/api/robot/manual-signals", json={
            "symbol": "1HZ100V", "direction": "CALL",
            "expiry_minutes": 5, "scheduled_for": when,
        })
        assert duplicate.status_code == 409
        assert len(client.get("/api/robot/manual-signals").json()["items"]) == 1
        # DB uniqueness is required even when separate workers race past the API check.
        from sqlalchemy.exc import IntegrityError
        import pytest
        with factory() as sess:
            stored = sess.get(db.RobotManualSignal, signal_id)
            sess.add(db.RobotManualSignal(
                user_id=a, symbol=stored.symbol, direction=stored.direction,
                expiry_minutes=stored.expiry_minutes, scheduled_for=stored.scheduled_for,
            ))
            with pytest.raises(IntegrityError):
                sess.commit()
            sess.rollback()

        client.cookies.set("nexora_sid", "draft_b")
        assert client.get("/api/robot/manual-signals").status_code == 409
        assert client.delete(f"/api/robot/manual-signals/{signal_id}").status_code == 404
        client.cookies.set("nexora_sid", "draft_a")
        with factory() as sess:
            planned = sess.get(db.RobotManualSignal, signal_id)
            planned.scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=1)
            sess.commit()
        expired = client.get("/api/robot/manual-signals").json()["items"][0]
        assert expired["status"] == "HORARIO_PASSOU_SEM_EXECUCAO"
        buy.assert_not_awaited()
        assert client.delete(f"/api/robot/manual-signals/{signal_id}").status_code == 200
        assert client.get("/api/robot/manual-signals").json()["items"] == []
    finally:
        engine.dispose()


def test_quick_wizard_read_is_independent_from_catalogue_and_broker(tmp_path, monkeypatch):
    client, _, engine, _ = planning_client(tmp_path, monkeypatch)
    try:
        unavailable = AsyncMock(side_effect=AssertionError("No broker call allowed in the wizard"))
        monkeypatch.setattr(main, "featured_instruments_by_group", unavailable)
        assert client.get("/api/robot/wizard").status_code == 200
        unavailable.assert_not_awaited()
    finally:
        engine.dispose()


def test_no_session_cannot_read_or_save_wizard(tmp_path, monkeypatch):
    client, _, engine, _ = planning_client(tmp_path, monkeypatch)
    try:
        client.cookies.clear()
        assert client.get("/api/robot/wizard").status_code == 401
        assert client.post("/api/robot/wizard", json=BASE).status_code == 401
    finally:
        engine.dispose()

def test_pending_wizard_draft_is_never_an_activation_permission(tmp_path, monkeypatch):
    """Both modes fail closed even if a legacy DEMO/REAL executor exists."""
    client, factory, engine, (a, _) = planning_client(tmp_path, monkeypatch)
    order = AsyncMock(side_effect=AssertionError("No broker endpoint should be called"))
    monkeypatch.setattr(main, "demo_buy", order)
    monkeypatch.setattr(main.real_runtime, "real_available", lambda: True)
    try:
        intelligent = {**BASE, "mode": "intelligent"}
        response = client.post("/api/robot/wizard", json=intelligent)
        assert response.status_code == 200, response.text
        status = client.get("/api/status").json()
        assert status["robot_usage_mode"] == "intelligent"
        assert status["wizard_draft_pending"] is True
        readiness = client.get("/api/autopilot/readiness")
        assert readiness.status_code == 200
        assert readiness.json()["ready"] is False
        assert "rascunho" in readiness.json()["reason"].lower()
        demo_start = client.post("/api/autopilot/start",
                                 json={"confirm": "START_DEMO_AUTOPILOT"})
        assert demo_start.status_code == 409
        assert "rascunho" in demo_start.json()["detail"].lower()
        real_start = client.post("/api/autopilot/start",
                                 json={"confirm": "START_REAL_AUTOPILOT"})
        assert real_start.status_code == 409
        assert "rascunho" in real_start.json()["detail"].lower()
        # The legacy endpoint may not silently retain a draft for the wrong mode.
        assert client.post("/api/robot/mode", json={"mode": "manual"}).status_code == 409
        with factory() as sess:
            assert sess.get(db.RobotUsageMode, a).mode == "intelligent"
            assert sess.get(db.RobotRun, a) is None
            assert sess.get(db.AutoPilotResumeConsent, a) is None
        order.assert_not_awaited()
    finally:
        engine.dispose()


def test_mode_and_draft_remain_private_in_status(tmp_path, monkeypatch):
    client, _, engine, _ = planning_client(tmp_path, monkeypatch)
    try:
        assert client.post("/api/robot/wizard", json=BASE).status_code == 200
        info = client.get("/api/status").json()
        assert info["robot_usage_mode"] == "manual"
        assert info["wizard_draft_pending"] is True
        client.cookies.set("nexora_sid", "draft_b")
        info = client.get("/api/status").json()
        assert info["robot_usage_mode"] == "intelligent"
        assert info["wizard_draft_pending"] is False
    finally:
        engine.dispose()


def test_dashboard_single_status_source_and_broker_offline_schedule():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "app/index.html").read_text()
    refresh = html.split("async function refreshDashboard(){", 1)[1].split(
        "let robotStartDialogResolver", 1)[0]
    assert "nxDashboardManualButton(status);" in refresh
    assert "nxWizardDraftPending&&!robotRunning" in refresh
    assert "RASCUNHO · NÃO ATIVADO" in refresh
    # The dashboard does not need to fetch a separate mode after /api/status.
    navigation = html.split("function go(v){", 1)[1].split("function fail(msg)", 1)[0]
    assert "nxDashboardManualButton();" not in navigation
    open_schedule = html.split("async function nxOpenManualSignals(){", 1)[1].split(
        "async function nxRefreshManualSignals(){", 1)[0]
    assert open_schedule.index("await nxRefreshManualSignals()") < open_schedule.index(
        "req('/api/automation/catalogue?market='")
    assert "Agenda salva disponível" in open_schedule


def test_saved_wizard_preview_requires_saved_draft_and_never_contacts_broker(tmp_path, monkeypatch):
    client, factory, engine, (a, b) = planning_client(tmp_path, monkeypatch)
    catalogue = AsyncMock(side_effect=AssertionError("No Deriv call during preview"))
    buy = AsyncMock(side_effect=AssertionError("No DEMO buy during preview"))
    monkeypatch.setattr(main, "featured_instruments_by_group", catalogue)
    monkeypatch.setattr(main, "demo_buy", buy)
    try:
        assert client.get("/api/robot/wizard/preview").status_code == 409
        assert client.post("/api/robot/wizard", json=BASE).status_code == 200
        response = client.get("/api/robot/wizard/preview")
        assert response.status_code == 200, response.text
        info = response.json()
        assert info["kind"] == "SAVED_DRAFT_EDUCATIONAL_PREVIEW"
        assert info["validation"] == "VALID_SAVED_DRAFT"
        assert info["mode"] == "manual"
        assert [x["status"] for x in info["stages"]] == [
            "SAVED_DRAFT", "SCHEDULE_ONLY", "NOT_CONNECTED"]
        for flag in ("broker_contacted", "market_data_used",
                     "results_simulated", "applied_to_executor"):
            assert info[flag] is False
        assert info["read_only"] is True
        assert info["orders_sent"] == 0
        assert info["predicted_outcome"] is None
        assert any("perdas" in note for note in info["notices"])
        with factory() as session:
            assert session.get(db.RobotPlan, a) is None
            assert session.get(db.RobotRun, a) is None
            assert session.get(db.AutoPilotResumeConsent, a) is None
        client.cookies.set("nexora_sid", "draft_b")
        assert client.get("/api/robot/wizard/preview").status_code == 409
        client.cookies.clear()
        assert client.get("/api/robot/wizard/preview").status_code == 401
        catalogue.assert_not_awaited()
        buy.assert_not_awaited()
    finally:
        engine.dispose()


def test_intelligent_preview_and_zero_stop_warning(tmp_path, monkeypatch):
    client, factory, engine, _ = planning_client(tmp_path, monkeypatch)
    try:
        payload = {**BASE, "mode": "intelligent", "recovery": "none",
                   "cycles": 0, "stop_loss": "0.00", "stop_win": "0.00"}
        assert client.post("/api/robot/wizard", json=payload).status_code == 200
        info = client.get("/api/robot/wizard/preview").json()
        assert info["mode"] == "intelligent"
        assert info["stages"][1]["status"] == "READ_ONLY_SEPARATE"
        assert len(info["notices"]) == 3
        assert any("Stop Loss" in note for note in info["notices"])
        assert any("Stop Win" in note for note in info["notices"])
        assert not any("Progressão" in note for note in info["notices"])
    finally:
        engine.dispose()


def test_read_only_intelligence_evidence_and_saved_draft_ui():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "app/index.html").read_text(
        encoding="utf-8")
    block = html.split("async function nxCheckResearchQuality(){", 1)[1].split(
        "let nxWizardLoadToken=", 1)[0]
    assert "PUBLIC_RESEARCH_CONTEXT_NOT_TRADING_SIGNALS" in block
    assert "DATA_COMPLETENESS_NOT_PROBABILITY" in block
    assert "item.observation_fingerprint" in block
    assert "panel.replaceChildren()" in block
    assert "panel.textContent" in block
    assert "/api/robot/wizard/preview" in block
    assert "results_simulated!==false" in block
    assert "orders_sent!==0" in block
    assert 'id="nxResearchQualityItems"' in html
    assert 'id="nxSavedDraftPreview"' in html
    assert "VERIFICAR RASCUNHO SALVO" in html

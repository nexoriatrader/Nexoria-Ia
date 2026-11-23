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

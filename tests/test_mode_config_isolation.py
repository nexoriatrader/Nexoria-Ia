"""P0-01: synthetic account/mode preference isolation. No broker requests."""
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db, main, real_runtime
from app.security import encrypt, hashsecret

DEMO = {
    "stake": "2.50", "stop_win": "12.00", "stop_loss": "8.00",
    "martingale": True, "gale_count": 2, "multiplier": "1.50",
    "cycles": "0", "cycle_multiplier": "2.00",
}
REAL = {
    "stake": "7.00", "stop_win": "19.00", "stop_loss": "14.00",
    "martingale": False, "gale_count": 0, "multiplier": "2.00",
    "cycles": "0", "cycle_multiplier": "2.00",
}


@pytest.fixture
def context(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/modes.sqlite")
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    monkeypatch.setenv("NEXORA_REAL_AUTOPILOT_ENABLED", "false")
    monkeypatch.setenv("NEXORA_REAL_RELEASE_APPROVED", "false")
    with sessions() as s:
        u = db.User(deriv_account_id="V_MODE_TEST", selected_mode="demo")
        s.add(u)
        s.flush()
        uid = u.id
        s.add(db.Session(
            session_hash=hashsecret("mode-sid"), user_id=uid,
            token_encrypted=encrypt("synthetic-oauth-token"),
            expires_at=db.utcnow() + timedelta(hours=2)))
        s.commit()
    client = TestClient(main.app)
    client.cookies.set("nexora_sid", "mode-sid")
    try:
        yield client, sessions, uid
    finally:
        main.runtimes.pop(uid, None)
        main.autopilot_enabled.discard(uid)
        real_runtime.running.discard(uid)
        engine.dispose()


def choose_real(sessions, uid, account_id):
    with sessions() as s:
        profile = s.get(db.AutomationProfile, uid)
        if profile is None:
            profile = db.AutomationProfile(user_id=uid)
            s.add(profile)
        profile.account_mode = "real"
        profile.real_account_id = account_id
        s.commit()


def choose_demo(sessions, uid):
    with sessions() as s:
        profile = s.get(db.AutomationProfile, uid)
        profile.account_mode = "demo"
        profile.real_account_id = None
        s.commit()


def test_demo_legacy_migrates_on_write_without_losing_progression(context):
    client, sessions, uid = context
    original = client.get("/api/onboarding/config").json()
    assert original["mode"] == "demo"
    assert original["revision"] == 0
    assert original["stake"] == "0.50"
    response = client.post("/api/onboarding/config", json=DEMO)
    assert response.status_code == 200, response.text
    current = client.get("/api/onboarding/config").json()
    assert current["stake"] == "2.50"
    assert current["gale_count"] == 2
    assert current["multiplier"] == "1.50"
    assert current["revision"] == 1
    with sessions() as s:
        user = s.get(db.User, uid)
        plan = s.get(db.RobotPlan, uid)
        snapshot = s.get(db.AccountRiskSettings, (uid, "demo", "V_MODE_TEST"))
        assert user.stake == "2.50"
        assert user.stop_loss == "8.00"
        assert plan.gale_count == 2 and plan.requested_martingale
        assert snapshot is not None and snapshot.martingale is True


def test_real_without_own_settings_does_not_inherit_demo(context):
    client, sessions, uid = context
    assert client.post("/api/onboarding/config", json=DEMO).status_code == 200
    choose_real(sessions, uid, "CRONE")
    response = client.get("/api/onboarding/config")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "real" and body["account_id"] == "CRONE"
    assert body["configured"] is False
    assert body["stake"] is None and body["stop_win"] is None and body["stop_loss"] is None
    assert body["martingale"] is False and body["cycles"] == "0"
    assert body["martingale_execution_enabled"] is False
    assert body["cycles_execution_enabled"] is False


def test_real_save_keeps_demo_fields_plan_and_engine_unchanged(context, monkeypatch):
    client, sessions, uid = context
    assert client.post("/api/onboarding/config", json=DEMO).status_code == 200
    demo_before = client.get("/api/onboarding/config").json()
    choose_real(sessions, uid, "CRONE")
    broker_buy = AsyncMock(side_effect=AssertionError("No external purchases"))
    monkeypatch.setattr(real_runtime, "buy_real_contract", broker_buy)
    monkeypatch.setattr(main, "demo_buy", broker_buy)
    response = client.post("/api/onboarding/config", json=REAL)
    assert response.status_code == 200, response.text
    assert response.json()["execution_mode"] == "DERIV_REAL_CONFIGURED_ONLY"
    real = client.get("/api/onboarding/config").json()
    assert real["stake"] == "7.00" and real["stop_loss"] == "14.00"
    assert real["revision"] == 1
    with sessions() as s:
        user = s.get(db.User, uid)
        plan = s.get(db.RobotPlan, uid)
        assert user.stake == demo_before["stake"]
        assert user.stop_win == demo_before["stop_win"]
        assert user.stop_loss == demo_before["stop_loss"]
        assert plan.requested_martingale and plan.gale_count == 2
        assert s.get(db.AccountRiskSettings, (uid, "real", "CRONE")) is not None
    choose_demo(sessions, uid)
    demo_after = client.get("/api/onboarding/config").json()
    for name in ("stake", "stop_win", "stop_loss", "martingale", "gale_count", "multiplier", "cycles"):
        assert demo_after[name] == demo_before[name]
    broker_buy.assert_not_awaited()


def test_multiple_real_accounts_never_copy_another_accounts_risk(context):
    client, sessions, uid = context
    choose_real(sessions, uid, "CRONE")
    assert client.post("/api/onboarding/config", json=REAL).status_code == 200
    choose_real(sessions, uid, "CRTWO")
    unconfigured = client.get("/api/onboarding/config").json()
    assert unconfigured["configured"] is False
    assert unconfigured["stake"] is None
    different = dict(REAL, stake="3.00", stop_win="9.00", stop_loss="6.00",
                     cycles="2", cycle_multiplier="1.25")
    assert client.post("/api/onboarding/config", json=different).status_code == 200
    assert client.get("/api/onboarding/config").json()["cycles"] == "2"
    choose_real(sessions, uid, "CRONE")
    original = client.get("/api/onboarding/config").json()
    assert original["stake"] == "7.00"
    assert original["cycles"] == "0"
    assert original["revision"] == 1
    choose_real(sessions, uid, "CRTWO")
    assert client.get("/api/onboarding/config").json()["stake"] == "3.00"


def test_real_progression_is_stored_only_as_inactive_preference(context):
    client, sessions, uid = context
    assert client.post("/api/onboarding/config", json=DEMO).status_code == 200
    choose_real(sessions, uid, "CRONE")
    different = dict(DEMO, stake="5.00", stop_win="10.00", stop_loss="8.00")
    assert client.post("/api/onboarding/config", json=different).status_code == 200
    got = client.get("/api/onboarding/config").json()
    assert got["martingale"] is True and got["gale_count"] == 2
    assert got["martingale_execution_enabled"] is False
    with sessions() as s:
        demo_plan = s.get(db.RobotPlan, uid)
        assert demo_plan.gale_count == 2
    # Both financial release gates stay OFF and settings never change them.
    assert real_runtime.real_available() is False


def test_legacy_config_endpoint_is_demo_only_even_if_profile_real(context):
    client, sessions, uid = context
    assert client.post("/api/onboarding/config", json=DEMO).status_code == 200
    choose_real(sessions, uid, "CRONE")
    assert client.post("/api/onboarding/config", json=REAL).status_code == 200
    legacy = client.post("/api/config", json={
        "stake": "4.00", "stop_win": "12.00", "stop_loss": "7.00",
        "martingale": False, "multiplier": "2.00"})
    assert legacy.status_code == 200, legacy.text
    assert client.get("/api/onboarding/config").json()["stake"] == "7.00"
    choose_demo(sessions, uid)
    assert client.get("/api/onboarding/config").json()["stake"] == "4.00"
    choose_real(sessions, uid, "CRONE")
    assert client.get("/api/onboarding/config").json()["stop_loss"] == "14.00"


def test_account_preferences_persist_across_new_session_and_revision(context):
    client, sessions, uid = context
    choose_real(sessions, uid, "CRONE")
    for version in (1, 2):
        result = client.post("/api/onboarding/config", json=REAL)
        assert result.status_code == 200
        fresh_client = TestClient(main.app)
        fresh_client.cookies.set("nexora_sid", "mode-sid")
        assert fresh_client.get("/api/onboarding/config").json()["revision"] == version
    with sessions() as fresh:
        assert fresh.get(db.AccountRiskSettings, (uid, "real", "CRONE")).revision == 2


def test_real_without_selected_account_fails_closed(context):
    client, sessions, uid = context
    with sessions() as s:
        s.add(db.AutomationProfile(user_id=uid, account_mode="real",
                                   real_account_id=None))
        s.commit()
    assert client.get("/api/onboarding/config").status_code == 409
    assert client.post("/api/onboarding/config", json=REAL).status_code == 409
    with sessions() as s:
        assert s.query(db.AccountRiskSettings).count() == 0


def test_real_start_cannot_use_demo_fallback_when_mode_config_missing(context, monkeypatch):
    client, sessions, uid = context
    assert client.post("/api/onboarding/config", json=DEMO).status_code == 200
    choose_real(sessions, uid, "CRONE")
    monkeypatch.setattr(real_runtime, "real_available", lambda: True)
    response = client.post("/api/autopilot/start", json={"confirm": "START_REAL_AUTOPILOT"})
    assert response.status_code == 409
    assert "Configure os limites REAL separadamente" in response.json()["detail"]
    with sessions() as s:
        assert s.query(db.RealOrder).count() == 0


def test_user_id_is_part_of_account_scoped_preferences(context):
    client, sessions, uid = context
    choose_real(sessions, uid, "CRSHAREDSYNTHETIC")
    assert client.post("/api/onboarding/config", json=REAL).status_code == 200
    with sessions() as s:
        second = db.User(deriv_account_id="V_OTHER_DEMO", selected_mode="demo")
        s.add(second)
        s.flush()
        other = second.id
        s.add(db.AutomationProfile(user_id=other, account_mode="real",
                                   real_account_id="CRSHAREDSYNTHETIC"))
        s.add(db.Session(session_hash=hashsecret("other-sid"), user_id=other,
                         token_encrypted=encrypt("other-synthetic-token"),
                         expires_at=db.utcnow() + timedelta(hours=2)))
        s.commit()
    second_client = TestClient(main.app)
    second_client.cookies.set("nexora_sid", "other-sid")
    assert second_client.get("/api/onboarding/config").json()["stake"] is None
    other_values = dict(REAL, stake="1.00", stop_win="3.00", stop_loss="2.00")
    assert second_client.post("/api/onboarding/config", json=other_values).status_code == 200
    assert second_client.get("/api/onboarding/config").json()["stake"] == "1.00"
    assert client.get("/api/onboarding/config").json()["stake"] == "7.00"
    with sessions() as s:
        assert s.get(db.AccountRiskSettings,(uid,"real","CRSHAREDSYNTHETIC")).stake == "7.00"
        assert s.get(db.AccountRiskSettings,(other,"real","CRSHAREDSYNTHETIC")).stake == "1.00"


def test_account_specific_preferences_dont_change_demo_legacy_endpoint(context):
    client, sessions, uid = context
    assert client.post("/api/onboarding/config", json=DEMO).status_code == 200
    choose_real(sessions, uid, "CRONE")
    assert client.post("/api/onboarding/config", json=REAL).status_code == 200
    old = client.get("/api/config/current").json()
    assert old["execution"] == "DERIV_DEMO_ONLY"
    assert old["stake"] == "2.50" and old["stop_loss"] == "8.00"


def test_mode_config_schema_is_additive_and_repeatable(context):
    client, sessions, uid = context
    choose_real(sessions, uid, "CRONE")
    assert client.post("/api/onboarding/config", json=REAL).status_code == 200
    with sessions() as s:
        database_engine = s.bind
        db.Base.metadata.create_all(database_engine, checkfirst=True)
        db.Base.metadata.create_all(database_engine, checkfirst=True)
        saved = s.get(db.AccountRiskSettings, (uid, "real", "CRONE"))
        assert saved.revision == 1
        assert saved.stake == "7.00"


def test_automation_ui_does_not_prefill_risk_across_unsaved_mode_switches():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "app" / "index.html").read_text()
    assert "function clearUnsavedAutomationRiskOnAccountChange()" in html
    clear = html.split("function clearUnsavedAutomationRiskOnAccountChange(){", 1)[1]
    clear = clear.split("async function changeAutomationMarket()", 1)[0]
    assert "['autoStake','autoWin','autoLoss']" in clear
    assert "restoreSelectedAutomationRisk()" in clear
    assert "$('autoRealAccount').addEventListener('change',()=>{" in clear
    assert "++automationRiskRequest" in clear
    assert "requestId!==automationRiskRequest" in clear
    assert "$('autoStake').value=risk.stake??'';" in html
    assert "$('autoWin').value=risk.stop_win??'';" in html
    assert "$('autoLoss').value=risk.stop_loss??'';" in html

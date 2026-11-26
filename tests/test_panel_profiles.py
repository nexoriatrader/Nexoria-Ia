"""Independent panel profiles, safe migration, risk isolation, and activation fencing."""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import db, main
from test_demopilot import cleanup, prepare


def test_panel_profile_migration_is_additive_and_idempotent(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/panel-migration.sqlite")
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", factory)

    with factory() as session:
        user = db.User(
            deriv_account_id="VPANELMIGRATION", selected_mode="real",
            stake="3.25", stop_win="12.00", stop_loss="7.00",
        )
        session.add(user)
        session.flush()
        session.add_all([
            db.AutomationProfile(
                user_id=user.id, operation_mode="manual", account_mode="real",
                market="forex", symbols_json="[]", strategy="manual",
                real_account_id="R123", expiry_minutes=0,
            ),
            db.RobotPlan(
                user_id=user.id, requested_martingale=False, gale_count=0,
                requested_multiplier="2.00", cycles_limit=2,
            ),
            db.RobotCycleParams(user_id=user.id, multiplier="2.50"),
            db.RealRobotConsent(
                user_id=user.id, account_id="R123", max_stake="3.25",
                stop_loss="7.00",
            ),
            db.DemoOrder(
                user_id=user.id, request_key="legacy-unattributed-order",
                state="SETTLED", symbol="1HZ100V", direction="CALL",
                duration_minutes=5, stake="1.00", pnl="0.50",
            ),
        ])
        session.commit()
        uid = user.id

    try:
        db.init_db()
        with factory() as session:
            manual = session.get(db.AutomationPanelProfile, (uid, "manual"))
            strategies = session.get(db.AutomationPanelProfile, (uid, "strategies"))
            assert manual.operation_mode == "manual"
            assert manual.account_mode == "real" and manual.real_account_id == "R123"
            assert manual.stake == "3.25" and manual.stop_loss == "7.00"
            assert manual.cycles_limit == 2 and manual.cycle_multiplier == "2.50"
            assert strategies.operation_mode == "strategies"
            assert strategies.account_mode == "demo"
            assert strategies.strategy == "pulso_v2"
            assert session.get(db.AutomationProfile, uid) is not None
            assert session.get(db.AutomationExecutionState, uid).active_panel_mode is None
            assert session.get(db.AutoPilotResumeConsent, uid) is None
            assert len(session.scalars(select(db.RealRobotConsent)).all()) == 1
            legacy_order = session.scalar(select(db.DemoOrder).where(
                db.DemoOrder.request_key == "legacy-unattributed-order"))
            assert legacy_order.source_panel_mode is None
            manual.stake = "4.75"
            session.commit()

        # A second startup must neither duplicate the rows nor overwrite a saved panel.
        db.init_db()
        with factory() as session:
            profiles = session.scalars(select(db.AutomationPanelProfile).where(
                db.AutomationPanelProfile.user_id == uid)).all()
            assert len(profiles) == 2
            assert session.get(db.AutomationPanelProfile, (uid, "manual")).stake == "4.75"
            assert session.get(db.AutomationPanelProfile, (uid, "strategies")).stake == "3.25"
            assert len(session.scalars(select(db.RealRobotConsent)).all()) == 1
    finally:
        engine.dispose()


def test_saving_one_panel_does_not_change_the_other_panel(tmp_path, monkeypatch):
    client, factory, engine, uid = prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "featured_instruments_by_group", AsyncMock(return_value=[
        {"symbol": "1HZ100V", "name": "Volatility 100 (1s)", "group": "derived"},
    ]))
    try:
        strategies_payload = {
            "panel_mode": "strategies", "operation_mode": "strategies",
            "account_mode": "demo", "market": "volatility",
            "symbols": ["1HZ100V"], "strategy": "pulso_v2",
            "expiry_minutes": 5, "stake": "1.25", "stop_win": "8.00",
            "stop_loss": "4.00", "martingale": False, "gale_count": 0,
            "cycles": 0, "multiplier": "2.00", "cycle_multiplier": "2.00",
        }
        saved_strategies = client.post("/api/automation/profile", json=strategies_payload)
        assert saved_strategies.status_code == 200, saved_strategies.text
        assert saved_strategies.json()["expiry_minutes"] == 5

        manual_payload = {
            "panel_mode": "manual", "operation_mode": "manual",
            "account_mode": "demo", "market": "volatility",
            "symbols": [], "strategy": "", "expiry_minutes": 0,
            "stake": "3.50", "stop_win": "15.00", "stop_loss": "9.00",
            "martingale": False, "gale_count": 0, "cycles": 0,
            "multiplier": "2.00", "cycle_multiplier": "2.00",
        }
        saved_manual = client.post("/api/automation/profile", json=manual_payload)
        assert saved_manual.status_code == 200, saved_manual.text

        manual = client.get("/api/automation/profile?panel_mode=manual").json()
        strategies = client.get("/api/automation/profile?panel_mode=strategies").json()
        assert (manual["stake"], manual["stop_win"], manual["stop_loss"]) == (
            "3.50", "15.00", "9.00")
        assert manual["symbols"] == [] and manual["strategy"] == "manual"
        assert manual["expiry_minutes"] == 0
        assert strategies["expiry_minutes"] == 5
        assert (strategies["stake"], strategies["stop_win"], strategies["stop_loss"]) == (
            "1.25", "8.00", "4.00")
        assert strategies["symbols"] == ["1HZ100V"] and strategies["strategy"] == "pulso_v2"

        # Editing Manual later still cannot mutate the Strategies profile.
        manual_payload["stake"] = "4.00"
        assert client.post("/api/automation/profile", json=manual_payload).status_code == 200
        strategies_after = client.get("/api/automation/profile?panel_mode=strategies").json()
        assert strategies_after["stake"] == "1.25"
        assert strategies_after["stop_loss"] == "4.00"
    finally:
        cleanup(engine)


def test_strategy_profile_automatically_uses_up_to_five_market_assets(tmp_path, monkeypatch):
    client, factory, engine, uid = prepare(tmp_path, monkeypatch)
    catalog = [
        {"symbol": symbol, "name": symbol, "group": "derived"}
        for symbol in ("R_10", "R_25", "R_50", "R_75", "R_100", "1HZ100V")
    ]
    monkeypatch.setattr(main, "featured_instruments_by_group", AsyncMock(return_value=catalog))
    try:
        response = client.post("/api/automation/profile", json={
            "panel_mode": "strategies", "operation_mode": "strategies",
            "account_mode": "demo", "market": "volatility",
            "symbols": [], "strategy": "pulso_v2", "expiry_minutes": 5,
            "direction": "PUT", "stake": "1.00", "stop_win": "0.00",
            "stop_loss": "0.00", "martingale": False, "gale_count": 0,
            "cycles": 0, "multiplier": "2.00", "cycle_multiplier": "2.00",
        })
        assert response.status_code == 200, response.text
        saved = client.get("/api/automation/profile?panel_mode=strategies").json()
        assert saved["symbols"] == ["R_10", "R_100", "R_25", "R_50", "R_75"]
        assert len(saved["symbols"]) == 5
        assert saved["direction"] == "AUTO"
    finally:
        cleanup(engine)


def test_forex_strategy_profile_requires_and_persists_m15(tmp_path, monkeypatch):
    client, factory, engine, uid = prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "featured_instruments_by_group", AsyncMock(return_value=[
        {"symbol": "frxEURUSD", "name": "EUR/USD", "group": "forex"},
    ]))
    payload = {
        "panel_mode": "strategies", "operation_mode": "strategies",
        "account_mode": "demo", "market": "forex", "symbols": [],
        "strategy": "trend", "expiry_minutes": 5,
        "stake": "1.00", "stop_win": "0.00", "stop_loss": "0.00",
        "martingale": False, "gale_count": 0, "cycles": 0,
        "multiplier": "2.00", "cycle_multiplier": "2.00",
    }
    try:
        for invalid_duration in (0, 1, 3, 5):
            payload["expiry_minutes"] = invalid_duration
            response = client.post("/api/automation/profile", json=payload)
            assert response.status_code == 422
            assert "M15" in response.json()["detail"]

        payload["expiry_minutes"] = 15
        response = client.post("/api/automation/profile", json=payload)
        assert response.status_code == 200, response.text
        saved = client.get("/api/automation/profile?panel_mode=strategies").json()
        assert saved["expiry_minutes"] == 15
        assert saved["symbols"] == ["frxEURUSD"]
    finally:
        cleanup(engine)


def test_risk_summary_only_counts_orders_from_requested_panel(tmp_path, monkeypatch):
    client, factory, engine, uid = prepare(tmp_path, monkeypatch)
    with factory() as session:
        session.add_all([
            db.DemoOrder(
                user_id=uid, request_key="manual-panel-win", source_panel_mode="manual",
                state="SETTLED", symbol="frxEURUSD", direction="CALL",
                duration_minutes=15, stake="2.00", pnl="1.50",
            ),
            db.DemoOrder(
                user_id=uid, request_key="strategy-panel-loss", source_panel_mode="strategies",
                state="SETTLED", symbol="1HZ100V", direction="PUT",
                duration_minutes=5, stake="2.00", pnl="-2.00",
            ),
            db.DemoOrder(
                user_id=uid, request_key="legacy-no-panel", source_panel_mode=None,
                state="SETTLED", symbol="R_100", direction="CALL",
                duration_minutes=5, stake="2.00", pnl="-9.00",
            ),
        ])
        session.commit()
    try:
        manual = client.get("/api/demo/risk-session?panel_mode=manual")
        strategies = client.get("/api/demo/risk-session?panel_mode=strategies")
        assert manual.status_code == strategies.status_code == 200
        assert manual.json()["pnl"] == "1.50"
        assert strategies.json()["pnl"] == "-2.00"
    finally:
        cleanup(engine)


@pytest.mark.parametrize(
    ("active_panel", "requested_panel", "active_label"),
    [
        ("manual", "strategies", "Nexora Manual"),
        ("strategies", "manual", "Nexora Estratégias"),
    ],
)
def test_opposite_panel_activation_returns_active_panel_name(
    tmp_path, monkeypatch, active_panel, requested_panel, active_label,
):
    client, factory, engine, uid = prepare(tmp_path, monkeypatch)
    buy = AsyncMock(side_effect=AssertionError("activation conflict must not buy"))
    monkeypatch.setattr(main, "demo_buy", buy)
    try:
        with factory() as session:
            lease = session.get(db.AutomationExecutionState, uid)
            lease.active_panel_mode = active_panel
            lease.account_mode = "demo"
            session.commit()

        response = client.post("/api/autopilot/start", json={
            "confirm": "START_DEMO_AUTOPILOT", "panel_mode": requested_panel,
        })
        assert response.status_code == 409, response.text
        assert active_label in response.json()["detail"]
        with factory() as session:
            assert session.get(db.AutomationExecutionState, uid).active_panel_mode == active_panel
        assert uid not in main.autopilot_enabled
        assert not buy.called
    finally:
        cleanup(engine)


def test_panel_apis_reject_requests_without_explicit_panel_mode(tmp_path, monkeypatch):
    client, factory, engine, uid = prepare(tmp_path, monkeypatch)
    buy = AsyncMock(side_effect=AssertionError("invalid panel requests must not buy"))
    monkeypatch.setattr(main, "demo_buy", buy)
    try:
        assert client.get("/api/automation/profile").status_code == 422
        assert client.get("/api/status").status_code == 422
        assert client.get("/api/status?panel_mode=unknown").status_code == 422
        assert client.post("/api/automation/profile", json={}).status_code == 422
        assert client.post("/api/autopilot/start", json={
            "confirm": "START_DEMO_AUTOPILOT",
        }).status_code == 422
        assert client.post("/api/autopilot/stop", json={}).status_code == 422
        assert client.get("/api/demo/risk-session").status_code == 422
        assert client.post("/api/demo/risk-session/reset", json={
            "confirm": "RESET_DEMO_RISK_SESSION",
        }).status_code == 422
        with factory() as session:
            assert session.get(db.AutomationExecutionState, uid).active_panel_mode is None
        assert not buy.called
    finally:
        cleanup(engine)

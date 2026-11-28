"""Dashboard read endpoints use worker threads and never dispatch trading."""
import threading
from datetime import timedelta
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db, main
from app.security import encrypt, hashsecret


def test_panel_read_endpoints_offload_sync_work_and_do_not_trade(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path}/dashboard.sqlite",
        connect_args={"check_same_thread": False},
    )
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(main.db, "SessionLocal", factory)

    sid = "dashboard-offload-session"
    with factory() as session:
        user = db.User(
            deriv_account_id="V-DASHBOARD-READONLY",
            selected_mode="demo",
            stake="2.00",
            stop_loss="10.00",
            stop_win="20.00",
        )
        session.add(user)
        session.flush()
        db.ensure_panel_profiles(session, user.id)
        session.add(db.Session(
            session_hash=hashsecret(sid),
            user_id=user.id,
            token_encrypted=encrypt("unused-in-read-test"),
            expires_at=db.utcnow() + timedelta(hours=1),
        ))
        session.commit()

    event_loop_threads = {}
    worker_threads = {}
    original_dashboard_to_thread = main.dashboard_to_thread

    async def tracked_dashboard_to_thread(stage, func, *args, **kwargs):
        event_loop_threads[stage] = threading.get_ident()

        def tracked_func(*inner_args, **inner_kwargs):
            worker_threads[stage] = threading.get_ident()
            return func(*inner_args, **inner_kwargs)

        return await original_dashboard_to_thread(
            stage, tracked_func, *args, **kwargs
        )

    monkeypatch.setattr(main, "dashboard_to_thread", tracked_dashboard_to_thread)
    buy = AsyncMock(side_effect=AssertionError("read endpoints must not trade"))
    monkeypatch.setattr(main, "demo_buy", buy)
    monkeypatch.setattr(main, "perform_demo_buy", buy)

    try:
        client = TestClient(main.app)
        client.cookies.set("nexora_sid", sid)
        status = client.get("/api/status?panel_mode=strategies&fresh=true")
        orders = client.get("/api/demo/orders")
        daily = client.get("/api/demo/results/today")
        manual_status = client.get("/api/manual-signals/status")
        readiness = client.get("/api/autopilot/readiness?panel_mode=strategies")
        access = client.get("/api/access/status")
        scanner = client.get("/api/autopilot/v2/scanner-status")
        evaluation = client.get("/api/autopilot/v2/evaluation")

        assert status.status_code == 200
        assert status.json()["status_is_stale"] is False
        assert orders.status_code == 200 and orders.json()["items"] == []
        assert daily.status_code == 200
        assert daily.json()["scope"] == "ALL_DEMO_TODAY"
        assert manual_status.status_code == 200
        assert manual_status.json()["configured"] is True
        assert readiness.status_code == 200
        assert readiness.json()["execution"] == "DERIV_DEMO_ONLY"
        assert access.status_code == 200
        assert "user_id" in access.json()
        assert scanner.status_code == 200
        assert evaluation.status_code == 200
        assert evaluation.json()["submitted"] == 0

        assert event_loop_threads
        assert event_loop_threads.keys() == worker_threads.keys()
        assert all(
            worker_threads[stage] != event_loop_threads[stage]
            for stage in event_loop_threads
        )
        buy.assert_not_awaited()
    finally:
        engine.dispose()

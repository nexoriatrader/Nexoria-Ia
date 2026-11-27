"""Fast authenticated dashboard snapshot behavior; no broker or trading calls."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main
from app import db
from app.security import encrypt, hashsecret


def _clear_user_snapshot(user_id):
    main.invalidate_status_cache(user_id)


def test_cold_status_returns_unconfirmed_while_background_snapshot_loads(monkeypatch):
    user_id = 987654321
    user = SimpleNamespace(id=user_id)

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        payload = {
            "running": True,
            "active_panel_mode": "strategies",
            "operation_mode": "strategies",
            "reason": "active in memory",
            "scan_progress": None,
        }

        async def slow_snapshot(_user, _panel_mode):
            started.set()
            await release.wait()
            return payload

        monkeypatch.setattr(main, "_build_status_snapshot", slow_snapshot)
        _clear_user_snapshot(user_id)

        pending = await main._get_status_snapshot(user, "strategies")
        assert pending["status_is_stale"] is True
        assert pending["status_updated_at"] is None
        assert pending["active_panel_mode"] is None
        assert pending["running"] is False
        assert "não confirmado" in pending["reason"].lower()

        key = main._status_cache_key(user_id, "strategies")
        refresh = main.status_snapshot_refresh_tasks[key]
        await asyncio.wait_for(started.wait(), timeout=1)
        assert not refresh.done()

        release.set()
        await asyncio.wait_for(refresh, timeout=1)
        latest = await main._get_status_snapshot(user, "strategies")
        assert latest["status_is_stale"] is False
        assert latest["active_panel_mode"] == "strategies"
        assert latest["running"] is True
        assert latest["status_updated_at"]

    try:
        asyncio.run(scenario())
    finally:
        _clear_user_snapshot(user_id)


def test_stale_snapshot_returns_immediately_and_fresh_mode_bypasses_cache(monkeypatch):
    user_id = 987654322
    user = SimpleNamespace(id=user_id)

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def snapshot(_user, _panel_mode):
            nonlocal calls
            calls += 1
            if calls > 1:
                started.set()
                await release.wait()
            return {
                "running": calls == 1,
                "active_panel_mode": "strategies" if calls == 1 else None,
                "operation_mode": "strategies",
                "reason": "fresh read " + str(calls),
                "scan_progress": None,
            }

        monkeypatch.setattr(main, "_build_status_snapshot", snapshot)
        _clear_user_snapshot(user_id)

        first = await main._get_status_snapshot(user, "strategies", fresh=True)
        assert first["status_is_stale"] is False
        assert first["running"] is True

        key = main._status_cache_key(user_id, "strategies")
        main.status_snapshot_cache[key]["refreshed_at"] -= (
            main.STATUS_SNAPSHOT_STALE_AFTER_SECONDS + 1
        )
        stale = await main._get_status_snapshot(user, "strategies")
        assert stale["status_is_stale"] is True
        assert stale["running"] is True  # Last known, explicitly not confirmed.
        assert stale["active_panel_mode"] == "strategies"

        refresh = main.status_snapshot_refresh_tasks[key]
        await asyncio.wait_for(started.wait(), timeout=1)
        release.set()
        await asyncio.wait_for(refresh, timeout=1)

        after_refresh = await main._get_status_snapshot(user, "strategies")
        assert after_refresh["status_is_stale"] is False
        assert after_refresh["running"] is False
        assert after_refresh["reason"] == "fresh read 2"

        forced = await main._get_status_snapshot(user, "strategies", fresh=True)
        assert calls == 3
        assert forced["status_is_stale"] is False
        assert forced["reason"] == "fresh read 3"

    try:
        asyncio.run(scenario())
    finally:
        _clear_user_snapshot(user_id)


def test_fresh_snapshot_reads_persisted_state_without_broker_calls(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path}/status.sqlite",
        connect_args={"check_same_thread": False},
    )
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)

    with factory() as session:
        user = db.User(
            deriv_account_id="V-SNAPSHOT-ONLY",
            selected_mode="demo",
            stake="2.00",
            stop_loss="10.00",
            stop_win="20.00",
        )
        session.add(user)
        session.flush()
        db.ensure_panel_profiles(session, user.id)
        session.add(db.Session(
            session_hash=hashsecret("snapshot-test-sid"),
            user_id=user.id,
            token_encrypted=encrypt("token-not-used"),
            expires_at=db.utcnow() + timedelta(hours=1),
        ))
        session.commit()
        user_id = user.id

    with factory() as session:
        detached_user = session.get(db.User, user_id)

    try:
        snapshot = asyncio.run(main._build_status_snapshot(detached_user, "strategies"))
        assert snapshot["running"] is False
        assert snapshot["active_panel_mode"] is None
        assert snapshot["configured_account_mode"] == "demo"
        assert snapshot["execution"] == "DERIV_DEMO_ONLY"
        assert snapshot["real_orders_enabled"] is main.real_runtime.real_available()
        with factory() as session:
            assert session.query(db.DemoOrder).count() == 0
    finally:
        main.runtimes.pop(user_id, None)
        engine.dispose()

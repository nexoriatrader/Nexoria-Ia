"""Read-only tenant isolation: synthetic SQLite data, no Deriv network or order execution."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db, main
from app.security import encrypt, hashsecret


@pytest.fixture
def isolated_clients(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path}/tenant-isolation.sqlite",
        connect_args={"check_same_thread": False},
    )
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    with factory() as session:
        for account, session_id, symbol, demo_pnl, real_pnl in (
            ("V_ISO_A", "session-isolation-A", "R_50", "1.00", "2.00"),
            ("V_ISO_B", "session-isolation-B", "R_75", "-1.00", "-2.00"),
        ):
            user = db.User(
                deriv_account_id=account,
                selected_mode="demo",
                stake="1.00",
                stop_win="10.00",
                stop_loss="5.00",
            )
            session.add(user)
            session.flush()
            session.add(db.Session(
                session_hash=hashsecret(session_id),
                user_id=user.id,
                token_encrypted=encrypt("synthetic-token"),
                expires_at=db.utcnow() + timedelta(hours=1),
            ))
            session.add(db.AutomationProfile(
                user_id=user.id,
                market="volatility",
                symbols_json='["' + symbol + '"]',
                strategy="trend",
                account_mode="demo",
            ))
            session.add(db.DemoOrder(
                user_id=user.id,
                request_key="synthetic-demo-" + account,
                state="SETTLED",
                contract_id="DEMO" + account,
                symbol=symbol,
                direction="CALL",
                duration_minutes=5,
                stake="1.00",
                pnl=demo_pnl,
            ))
            session.add(db.RealOrder(
                user_id=user.id,
                account_id="REAL" + account,
                request_key="synthetic-real-" + account,
                state="SETTLED",
                contract_id="REAL_CONTRACT" + account,
                symbol=symbol,
                direction="PUT",
                duration_minutes=5,
                stake="1.00",
                currency="USD",
                pnl=real_pnl,
            ))
        session.commit()
    try:
        yield factory
    finally:
        engine.dispose()


def client_with_session(session_id=None):
    client = TestClient(main.app)
    if session_id:
        client.cookies.set("nexora_sid", session_id)
    return client


@pytest.mark.parametrize("path", (
    "/api/status",
    "/api/automation/profile",
    "/api/demo/orders",
    "/api/real/orders",
))
def test_private_read_routes_reject_anonymous_session(isolated_clients, path):
    response = client_with_session().get(path)
    assert response.status_code == 401
    assert "no-store" in response.headers.get("cache-control", "").lower()


@pytest.mark.parametrize("path, expected_key", (
    ("/api/automation/profile", "symbols"),
    ("/api/demo/orders", "items"),
    ("/api/real/orders", "items"),
))
def test_private_read_routes_are_scoped_to_authenticated_user(isolated_clients, path, expected_key):
    # Supplying the other user's id in the query must not override cookie identity.
    with isolated_clients() as s:
        user_b = s.query(db.User).filter_by(deriv_account_id="V_ISO_B").one()
        foreign_id = user_b.id
    results = []
    for sid, expected_symbol, other_symbol in (
        ("session-isolation-A", "R_50", "R_75"),
        ("session-isolation-B", "R_75", "R_50"),
    ):
        response = client_with_session(sid).get(path, params={"user_id": foreign_id})
        assert response.status_code == 200
        body = response.json()
        if expected_key == "symbols":
            symbols = body["symbols"]
        else:
            symbols = [item["symbol"] for item in body["items"]]
        assert symbols == [expected_symbol]
        assert other_symbol not in symbols
        results.append(body)
    assert results[0] != results[1]


def test_revoked_session_stops_read_access(isolated_clients):
    client = client_with_session("session-isolation-A")
    assert client.get("/api/demo/orders").status_code == 200
    with isolated_clients() as s:
        s.query(db.Session).filter_by(
            session_hash=hashsecret("session-isolation-A")
        ).delete()
        s.commit()
    assert client.get("/api/demo/orders").status_code == 401
    assert client.get("/api/real/orders").status_code == 401

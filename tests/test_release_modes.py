"""Release regression guards: truthful REAL availability and immutable running configuration."""
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db, main, real_runtime
from app.security import encrypt, hashsecret


def client_with_user(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/release.sqlite")
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    with factory() as sess:
        user = db.User(deriv_account_id="VRELEASE01", selected_mode="demo")
        sess.add(user)
        sess.flush()
        uid = user.id
        sess.add(db.Session(
            session_hash=hashsecret("release-session"), user_id=uid,
            token_encrypted=encrypt("mock-token"),
            expires_at=db.utcnow() + timedelta(hours=1),
        ))
        sess.commit()
    client = TestClient(main.app)
    client.cookies.set("nexora_sid", "release-session")
    return client, engine, uid


def test_public_and_authenticated_real_flags_agree(tmp_path, monkeypatch):
    client, engine, uid = client_with_user(tmp_path, monkeypatch)
    try:
        monkeypatch.delenv("NEXORA_REAL_AUTOPILOT_ENABLED", raising=False)
        assert client.get("/healthz").json()["real_orders_enabled"] is False
        assert client.get("/api/session").json()["real_enabled"] is False
        assert client.get("/api/onboarding/config").json()["real_enabled"] is False
        monkeypatch.setenv("NEXORA_REAL_AUTOPILOT_ENABLED", "true")
        health = client.get("/healthz").json()
        assert health["real_orders_enabled"] is True
        assert "real" in health["execution_modes"]
        assert client.get("/api/session").json()["real_enabled"] is True
        assert client.get("/api/onboarding/config").json()["real_enabled"] is True
        assert client.get("/api/status").json()["real_orders_enabled"] is True
    finally:
        real_runtime.running.discard(uid)
        engine.dispose()


def test_real_run_blocks_onboarding_edits(tmp_path, monkeypatch):
    client, engine, uid = client_with_user(tmp_path, monkeypatch)
    payload = {
        "stake": "2", "stop_win": "6", "stop_loss": "4",
        "martingale": False, "gale_count": 0,
        "multiplier": "2", "cycles": "0", "cycle_multiplier": "2",
    }
    try:
        previous = client.get("/api/onboarding/config").json()
        real_runtime.running.add(uid)
        response = client.post("/api/onboarding/config", json=payload)
        assert response.status_code == 409
        assert "Desligue o robô" in response.json()["detail"]
        current = client.get("/api/onboarding/config").json()
        assert current["stake"] == previous["stake"]
        assert current["stop_loss"] == previous["stop_loss"]
    finally:
        real_runtime.running.discard(uid)
        engine.dispose()


def test_initial_setup_explains_real_requires_separate_explicit_opt_in():
    html = (Path(__file__).resolve().parents[1] / "app" / "index.html").read_text(encoding="utf-8")
    assert 'id="setupRealAvailability"' in html
    assert "s.real_enabled?" in html
    assert "Disponível na seção Automação mediante confirmação" in html
    assert "Aguardando habilitação do servidor" in html
    assert "START_REAL_AUTOPILOT" in html

def test_logout_revokes_real_consent_and_disarms_real_worker(tmp_path, monkeypatch):
    client, engine, uid = client_with_user(tmp_path, monkeypatch)
    try:
        with db.SessionLocal() as sess:
            sess.add(db.RealRobotConsent(
                user_id=uid, account_id="R001",
                max_stake="1.00", stop_loss="5.00"))
            sess.commit()
        real_runtime.running.add(uid)
        response = client.post("/api/logout")
        assert response.status_code == 200
        assert uid not in real_runtime.running
        with db.SessionLocal() as sess:
            assert sess.get(db.RealRobotConsent, uid).revoked_at is not None
            assert sess.get(db.Session, hashsecret("release-session")) is None
    finally:
        real_runtime.running.discard(uid)
        engine.dispose()


def test_real_confirmation_explains_cash_risk_without_changing_demo_text():
    html = (Path(__file__).resolve().parents[1] / "app" / "index.html").read_text(encoding="utf-8")
    assert 'id="robotStartDialogWarning"' in html
    assert "profile.account_mode==='real'?" in html
    assert "DINHEIRO REAL" in html
    assert "profile.real_account_id" in html
    assert "conta DEMO conforme os parâmetros salvos" in html

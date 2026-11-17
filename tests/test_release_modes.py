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
        monkeypatch.delenv("NEXORA_REAL_RELEASE_APPROVED", raising=False)
        assert client.get("/healthz").json()["real_orders_enabled"] is False
        assert client.get("/api/session").json()["real_enabled"] is False
        assert client.get("/api/onboarding/config").json()["real_enabled"] is False
        # The legacy flag alone cannot enable real-money purchases.
        monkeypatch.setenv("NEXORA_REAL_AUTOPILOT_ENABLED", "true")
        assert client.get("/healthz").json()["real_orders_enabled"] is False
        assert client.get("/api/session").json()["real_enabled"] is False
        monkeypatch.setenv("NEXORA_REAL_RELEASE_APPROVED", "true")
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

def test_real_orders_never_start_with_only_one_release_flag(monkeypatch):
    """The independent launch gate is fail-closed, regardless of prior flags."""
    monkeypatch.setenv("NEXORA_REAL_AUTOPILOT_ENABLED","true")
    monkeypatch.delenv("NEXORA_REAL_RELEASE_APPROVED",raising=False)
    assert real_runtime.real_available() is False
    monkeypatch.setenv("NEXORA_REAL_RELEASE_APPROVED","TRUE")
    assert real_runtime.real_available() is True
    monkeypatch.setenv("NEXORA_REAL_AUTOPILOT_ENABLED","false")
    assert real_runtime.real_available() is False



def test_beta_real_start_rejects_oversized_stake_before_deriv_network(tmp_path, monkeypatch):
    """Even an opted-in user cannot bypass the release pilot cap."""
    import json
    client, engine, uid = client_with_user(tmp_path, monkeypatch)
    monkeypatch.setenv('NEXORA_REAL_AUTOPILOT_ENABLED','true')
    monkeypatch.setenv('NEXORA_REAL_RELEASE_APPROVED','true')
    monkeypatch.delenv('NEXORA_REAL_MAX_STAKE_USD',raising=False)
    try:
        with db.SessionLocal() as sess:
            user=sess.get(db.User,uid)
            user.stake='2'
            user.stop_loss='5'
            user.selected_mode='real'
            sess.add(db.AutomationProfile(
                user_id=uid,account_mode='real',real_account_id='CR123',
                market='derived',symbols_json=json.dumps(['R_100']),
                strategy='trend',expiry_minutes=0,indicators_json='[]',
                direction='AUTO',min_payout='88',news_filter=False,
            ))
            sess.commit()
        res=client.post('/api/autopilot/start',
                        json={'confirm':'START_REAL_AUTOPILOT'})
        assert res.status_code==409
        assert 'no máximo US$ 1.00' in res.json()['detail']
        assert uid not in real_runtime.running
    finally:
        real_runtime.running.discard(uid)
        engine.dispose()

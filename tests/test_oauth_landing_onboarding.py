"""Regression tests for first Deriv login, callback replays and 24h landing."""
import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db, main
from app.security import encrypt, hashsecret


def local_client(tmp_path, monkeypatch):
    engine=create_engine(f"sqlite:///{tmp_path}/oauth-landing.sqlite")
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,"SessionLocal",factory)
    monkeypatch.setenv("NEXORA_PUBLIC_URL","http://localhost:8000")
    return TestClient(main.app),factory,engine


def test_public_offer_is_24h_by_default_and_referrals_remain_one_day(monkeypatch):
    monkeypatch.delenv("NEXORA_TRIAL_HOURS",raising=False)
    with TestClient(main.app) as client:
        r=client.get("/api/public/offer")
        assert r.status_code==200
        assert r.json()=={
            "trial_hours":24,
            "trial_starts":"first_verified_deriv_login",
            "referral_bonus_hours":24,
        }
    # An explicit legacy grant is still readable without changing saved grants.
    monkeypatch.setenv("NEXORA_TRIAL_HOURS","168")
    with TestClient(main.app) as client:
        assert client.get("/api/public/offer").json()["trial_hours"]==168


def test_consumed_oauth_callback_preserves_existing_valid_session(tmp_path,monkeypatch):
    client,factory,engine=local_client(tmp_path,monkeypatch)
    with factory() as sess:
        user=db.User(deriv_account_id="VREPLAY")
        sess.add(user);sess.flush()
        sess.add(db.Session(session_hash=hashsecret("valid-sid"),
                            user_id=user.id,token_encrypted=encrypt("token-test"),
                            expires_at=db.utcnow()+timedelta(hours=1)))
        sess.commit()
    try:
        client.cookies.set("nexora_sid","valid-sid")
        client.cookies.set("nexora_state","single-use-state")
        r=client.get("/auth/deriv/callback",
            params={"state":"single-use-state","code":"already-consumed"},
            follow_redirects=False)
        assert r.status_code==303
        assert r.headers["location"]=="/?login=existing"
        assert client.get("/api/session").json()["authenticated"] is True
    finally:
        client.close();engine.dispose()


def test_expired_or_invalid_callback_never_creates_a_session(tmp_path,monkeypatch):
    client,factory,engine=local_client(tmp_path,monkeypatch)
    try:
        client.cookies.set("nexora_state","expired-state")
        r=client.get("/auth/deriv/callback",
            params={"state":"expired-state","code":"outdated"},
            follow_redirects=False)
        assert r.status_code==303
        assert r.headers["location"]=="/?login=expired"
        assert client.get("/api/session").json()["authenticated"] is False
        # A deliberately mismatched state must not create or reuse any login.
        client.cookies.set("nexora_state","other-state")
        r=client.get("/auth/deriv/callback",
            params={"state":"wrong-state","code":"outdated"},
            follow_redirects=False)
        assert r.status_code==303
        assert r.headers["location"]=="/?login=expired"
    finally:
        client.close();engine.dispose()


def test_stale_oauth_flow_is_consumed_safely_without_token_exchange(tmp_path,monkeypatch):
    client,factory,engine=local_client(tmp_path,monkeypatch)
    with factory() as sess:
        sess.add(db.LoginFlow(
            state_hash=hashsecret("old-state"),
            verifier_encrypted=encrypt("old-verifier"),
            created_at=db.utcnow()-timedelta(minutes=11),
        ))
        sess.commit()
    try:
        client.cookies.set("nexora_state","old-state")
        r=client.get("/auth/deriv/callback",params={
            "state":"old-state","code":"expired-code"},follow_redirects=False)
        assert r.status_code==303 and r.headers["location"]=="/?login=expired"
        with factory() as sess:
            assert sess.get(db.LoginFlow,hashsecret("old-state")) is None
            assert sess.query(db.Session).count()==0
    finally:
        client.close();engine.dispose()


def test_successful_oauth_followed_by_refresh_does_not_show_login_error(tmp_path,monkeypatch):
    client,factory,engine=local_client(tmp_path,monkeypatch)
    monkeypatch.setenv("NEXORA_ACCESS_CONTROL_ENABLED","true")
    monkeypatch.setenv("NEXORA_TRIAL_HOURS","24")

    class Response:
        def __init__(self, body):self.body=body
        def raise_for_status(self):pass
        def json(self):return self.body

    class FakeDeriv:
        def __init__(self,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return False
        async def post(self,url,**kw):
            return Response({"access_token":"fixture-token","expires_in":3600})
        async def get(self,url,**kw):
            return Response({"data":[{"account_type":"demo","account_id":"VRTC5555"}]})

    monkeypatch.setattr(main.httpx,"AsyncClient",FakeDeriv)
    try:
        auth=client.get("/auth/deriv/start",follow_redirects=False)
        assert auth.status_code==302
        state=parse_qs(urlsplit(auth.headers["location"]).query)["state"][0]
        accepted=client.get("/auth/deriv/callback",params={
            "state":state,"code":"fixture-code"},follow_redirects=False)
        assert accepted.status_code==303
        assert accepted.headers["location"]=="/?login=ok"
        assert client.get("/api/session").json()["authenticated"] is True
        with factory() as sess:
            user=sess.query(db.User).one()
            grant=sess.get(db.RobotAccess,user.id)
            assert grant.access_kind=="trial_24h"
            assert grant.expires_at-grant.starts_at==timedelta(hours=24)
        # Browser history/reload of the one-use callback must keep that session.
        replay=client.get("/auth/deriv/callback",params={
            "state":state,"code":"fixture-code"},follow_redirects=False)
        assert replay.status_code==303
        assert replay.headers["location"]=="/?login=existing"
        with factory() as sess:
            assert sess.query(db.User).count()==1
            assert sess.query(db.Session).count()==1
    finally:
        client.close();engine.dispose()


def test_landing_offers_24h_and_new_configuration_wizard():
    html=(Path(__file__).resolve().parents[1]/"app"/"index.html").read_text(encoding="utf-8")
    assert 'Teste grátis por <span data-trial-label>24 horas</span>' in html
    assert '<strong><span data-trial-label>24 horas</span> grátis</strong>' in html
    assert 'id="lpLoginNotice"' in html
    assert "if(loginResult==='ok'){" in html
    assert "go('robot-config');" in html
    assert "go('setup')" not in html
    assert "showLandingLoginNotice(loginResult);" in html
    assert "const data=await req('/api/public/offer');" in html
    assert "R$ 29,90" in html and "R$ 49,90" in html and "R$ 99,00" in html
    assert "não são garantidos" not in html or "não garante lucros" in html


def test_inline_browser_script_has_valid_javascript_syntax(tmp_path):
    node=shutil.which("node")
    if node is None:
        return
    html=(Path(__file__).resolve().parents[1]/"app"/"index.html").read_text(encoding="utf-8")
    script=html.split("<script>",1)[1].split("</script>",1)[0]
    js_file=tmp_path/"nexora-inline.js"
    js_file.write_text(script,encoding="utf-8")
    checked=subprocess.run([node,"--check",str(js_file)],
        capture_output=True,text=True,timeout=10,check=False)
    assert checked.returncode==0,checked.stderr

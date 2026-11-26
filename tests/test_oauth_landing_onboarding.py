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


def test_new_users_start_with_half_dollar_while_existing_settings_remain(tmp_path):
    engine=create_engine(f"sqlite:///{tmp_path}/stake.sqlite")
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine)
    try:
        with factory() as sess:
            new=db.User(deriv_account_id="DEFAULT-050")
            existing=db.User(deriv_account_id="SAVED-200",stake="2.00")
            sess.add_all([new,existing])
            sess.commit()
            assert sess.get(db.User,new.id).stake=="0.50"
            assert sess.get(db.User,existing.id).stake=="2.00"
    finally:
        engine.dispose()


def test_deriv_oauth_partner_token_on_existing_login_and_signup(monkeypatch):
    from app.security import build_authorize_url, REDIRECT
    # The same tracking token from NEXORA's external Deriv partner link is
    # now carried directly through OAuth, including the single-button flow.
    monkeypatch.setenv("DERIV_AFFILIATE_TRACKING_TOKEN","HR6MZB6Q499Z")
    monkeypatch.setenv("DERIV_AFFILIATE_UTM_CAMPAIGN","nexora_ia")
    monkeypatch.delenv("DERIV_AFFILIATE_UTM_SOURCE",raising=False)
    for register in (False, True):
        url=build_authorize_url("safe-state","random-verifier",register=register)
        query=parse_qs(urlsplit(url).query)
        assert query["t"]==["HR6MZB6Q499Z"]
        assert query["utm_medium"]==["affiliate"]
        assert query["utm_campaign"]==["nexora_ia"]
        assert "utm_source" not in query
        assert query["redirect_uri"]==[REDIRECT]
        assert query["state"]==["safe-state"]
        assert query["code_challenge_method"]==["S256"]
        assert ("prompt" in query)==register
    monkeypatch.setenv("DERIV_AFFILIATE_UTM_SOURCE","VERIFIED_PARTNER_ID")
    query=parse_qs(urlsplit(build_authorize_url("state","verifier")).query)
    assert query["utm_source"]==["VERIFIED_PARTNER_ID"]


def test_deriv_oauth_does_not_include_unvalidated_tracking_values(monkeypatch):
    from app.security import build_authorize_url
    monkeypatch.setenv("DERIV_AFFILIATE_TRACKING_TOKEN","INVALID&redirect_uri=bad")
    query=parse_qs(urlsplit(build_authorize_url("state","verifier")).query)
    assert "t" not in query
    assert "utm_medium" not in query


def test_deriv_first_signup_uses_registered_callback_and_preserves_referral(tmp_path,monkeypatch):
    from app.security import build_authorize_url,REDIRECT
    client,factory,engine=local_client(tmp_path,monkeypatch)
    try:
        response=client.get('/auth/deriv/start',
            params={'register':'true','ref':'ABCD1234'},follow_redirects=False)
        assert response.status_code==302
        qs=parse_qs(urlsplit(response.headers['location']).query)
        assert qs['prompt']==['registration']
        assert qs['redirect_uri']==[REDIRECT]
        assert qs['state'][0]
        from app.security import hashsecret
        with factory() as sess:
            flow=sess.get(db.LoginFlow,hashsecret(qs['state'][0]))
            assert flow.referral_code=='ABCD1234'
        assert 'nexora_state=' in response.headers.get('set-cookie','')
    finally:
        client.close();engine.dispose()


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
    welcome=html.split('<section id="welcome"',1)[1].split('<section id="auth-guide"',1)[0]
    assert 'Teste grátis por <span data-trial-label>24 horas</span>' in html
    assert '<strong><span data-trial-label>24 horas</span> grátis</strong>' in html
    assert 'id="lpLoginNotice"' in html
    assert "if(loginResult==='ok'||window.location.pathname==='/boas-vindas'){" in html
    assert "go('app-home');" in html
    assert "go('setup')" not in html
    assert "showLandingLoginNotice(loginResult);" in html
    assert "window.location.pathname==='/boas-vindas'" in html
    assert "const data=await req('/api/public/offer');" in html
    assert "R$ 29,90" in html and "R$ 49,90" in html and "R$ 99,00" in html
    assert "O Nexora não garante lucro." in welcome
    assert "funciona em Treinamento e em conta REAL" in welcome
    assert "apenas em DEMO" not in welcome


def test_public_welcome_route_serves_the_educational_auth_page(tmp_path,monkeypatch):
    client,factory,engine=local_client(tmp_path,monkeypatch)
    try:
        response=client.get('/boas-vindas')
        assert response.status_code==200
        assert '<section id="auth-guide"' in response.text
        assert 'Depois de concluir o cadastro ou login e a autorização' in response.text
        assert 'Já tenho conta — entrar' in response.text
    finally:
        client.close();engine.dispose()


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


def test_start_login_prunes_old_single_use_flows(tmp_path,monkeypatch):
    client,factory,engine=local_client(tmp_path,monkeypatch)
    with factory() as sess:
        sess.add(db.LoginFlow(
            state_hash=hashsecret('stale'),
            verifier_encrypted=encrypt('stale-verifier'),
            created_at=db.utcnow()-timedelta(minutes=17)))
        sess.add(db.LoginFlow(
            state_hash=hashsecret('recent'),
            verifier_encrypted=encrypt('recent-verifier'),
            created_at=db.utcnow()-timedelta(minutes=2)))
        sess.commit()
    try:
        r=client.get('/auth/deriv/start',follow_redirects=False)
        assert r.status_code==302
        with factory() as sess:
            assert sess.get(db.LoginFlow,hashsecret('stale')) is None
            assert sess.get(db.LoginFlow,hashsecret('recent')) is not None
            assert sess.query(db.LoginFlow).count()==2
    finally:
        client.close();engine.dispose()

"""P0-01 regression: atomic profile/risk edits are data-only, fail closed.

All users, orders and market data are synthetic. No Deriv or external network.
"""
from datetime import timedelta
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import pytest

from app import db, main, real_runtime
from app.security import encrypt, hashsecret


PROFILE={
    "account_mode":"demo","market":"derived","symbols":["1HZ100V"],
    "strategy":"pulso_v2","expiry_minutes":0,"news_filter":False,
    "news_minutes_before":30,"news_minutes_after":30,"max_daily_orders":0,
    "indicators":["ATR"],"direction":"AUTO","min_payout_pct":88,
    "real_account_id":None,"preset_mode":"custom"
}
RISK={
    "stake":"2.50","stop_win":"8.00","stop_loss":"6.00",
    "martingale":False,"gale_count":0,"multiplier":"2.00",
    "cycles":"0","cycle_multiplier":"2.00"
}

@pytest.fixture
def context(tmp_path,monkeypatch):
    engine=create_engine(f"sqlite:///{tmp_path}/atomic.sqlite")
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,"SessionLocal",factory)
    monkeypatch.setenv("NEXORA_REAL_AUTOPILOT_ENABLED","false")
    monkeypatch.setenv("NEXORA_REAL_RELEASE_APPROVED","false")
    monkeypatch.setattr(main,"featured_instruments_by_group",
        AsyncMock(return_value=[{"symbol":"1HZ100V","name":"Synthetic","group":"derived"}]))
    demo_broker=AsyncMock(side_effect=AssertionError("No broker calls in config tests"))
    real_broker=AsyncMock(side_effect=AssertionError("No broker calls in config tests"))
    monkeypatch.setattr(main,"demo_buy",demo_broker)
    monkeypatch.setattr(real_runtime,"buy_real_contract",real_broker)
    with factory() as s:
        user=db.User(deriv_account_id="V_ATOMIC",selected_mode="demo")
        s.add(user);s.flush()
        uid=user.id
        s.add(db.Session(session_hash=hashsecret("atomic-sid"),user_id=uid,
            token_encrypted=encrypt("synthetic-token"),
            expires_at=db.utcnow()+timedelta(hours=1)))
        s.commit()
    client=TestClient(main.app,raise_server_exceptions=False)
    client.cookies.set("nexora_sid","atomic-sid")
    try:
        yield client,factory,uid,demo_broker,real_broker
    finally:
        main.runtimes.pop(uid,None)
        main.autopilot_enabled.discard(uid)
        real_runtime.running.discard(uid)
        engine.dispose()


def test_bundle_saves_demo_profile_limits_and_progression_together(context):
    client,sessions,uid,demo_broker,real_broker=context
    response=client.post("/api/automation/configuration",
                         json={"profile":PROFILE,"risk":RISK})
    assert response.status_code==200,response.text
    assert response.json()["risk_saved"] is True
    with sessions() as s:
        user=s.get(db.User,uid)
        profile=s.get(db.AutomationProfile,uid)
        settings=s.get(db.AccountRiskSettings,(uid,"demo","V_ATOMIC"))
        assert user.stake=="2.50" and user.stop_loss=="6.00"
        assert profile.account_mode=="demo" and profile.strategy=="pulso_v2"
        assert settings.stake=="2.50" and settings.stop_loss=="6.00"
        assert s.get(db.RobotPlan,uid).gale_count==0
    assert client.get("/api/config/current").json()["stop_loss"]=="6.00"
    demo_broker.assert_not_awaited()
    real_broker.assert_not_awaited()


def test_bundle_invalid_risk_does_not_persist_profile(context):
    client,sessions,uid,_,_=context
    invalid=dict(RISK,stake="0.00")
    response=client.post("/api/automation/configuration",
                         json={"profile":PROFILE,"risk":invalid})
    assert response.status_code==422,response.text
    with sessions() as s:
        assert s.get(db.AutomationProfile,uid) is None
        assert s.get(db.User,uid).stake=="0.50"
        assert s.query(db.AccountRiskSettings).count()==0


def test_bundle_rolls_back_both_on_failure_after_staging_profile(context,monkeypatch):
    client,sessions,uid,_,_=context
    first=client.post("/api/automation/configuration",json={"profile":PROFILE,"risk":RISK})
    assert first.status_code==200
    old_main=main._apply_automation_profile
    def inject_error(session,user,payload):
        old_main(session,user,payload)
        session.flush()
        raise RuntimeError("synthetic failure AFTER profile flush, before commit")
    monkeypatch.setattr(main,"_apply_automation_profile",inject_error)
    changed_profile={**PROFILE,"strategy":"trend"}
    changed_risk={**RISK,"stake":"5.00","stop_loss":"12.00"}
    result=client.post("/api/automation/configuration",
        json={"profile":changed_profile,"risk":changed_risk})
    assert result.status_code==500
    with sessions() as s:
        assert s.get(db.AutomationProfile,uid).strategy=="pulso_v2"
        assert s.get(db.User,uid).stake=="2.50"
        assert s.get(db.AccountRiskSettings,(uid,"demo","V_ATOMIC")).stake=="2.50"
    # No partial registration or authorization persisted.


def test_bundle_rejects_running_robot_without_mutation(context):
    client,sessions,uid,_,_=context
    main.autopilot_enabled.add(uid)
    try:
        response=client.post("/api/automation/configuration",
            json={"profile":PROFILE,"risk":RISK})
        assert response.status_code==409
        with sessions() as s:
            assert s.get(db.AutomationProfile,uid) is None
    finally:
        main.autopilot_enabled.discard(uid)


def test_legacy_profile_endpoint_is_still_available(context):
    client,sessions,uid,_,_=context
    response=client.post("/api/automation/profile",json=PROFILE)
    assert response.status_code==200,response.text
    assert response.json()["configured"] is True
    with sessions() as s:
        assert s.get(db.AutomationProfile,uid).account_mode=="demo"


def test_automation_ui_uses_single_atomic_wizard_save():
    from pathlib import Path
    source=(Path(__file__).resolve().parents[1]/"app"/"index.html").read_text()
    section=source.split("async function saveAutomationProfile(){",1)[1].split("async function loadSettings(){",1)[0]
    assert "await req('/api/automation/configuration'" in section
    assert "JSON.stringify({profile,risk})" in section
    assert "await req('/api/automation/profile'" not in section
    assert "await req('/api/onboarding/config'" not in section


def test_bundle_real_preferences_are_isolated_and_never_authorize_trading(context,monkeypatch):
    client,sessions,uid,demo_broker,real_broker=context
    class AccountsResult:
        def raise_for_status(self): pass
        def json(self):
            return {"data":{"accounts":[{"account_type":"real","account_id":"CRONE","currency":"USD"}]}}
    class MockAccountClient:
        def __init__(self,*args,**kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return None
        async def get(self,url,headers=None):
            assert url.endswith("/trading/v1/options/accounts")
            return AccountsResult()
    monkeypatch.setattr(main.httpx,"AsyncClient",MockAccountClient)
    real_profile={**PROFILE,"account_mode":"real","real_account_id":"CRONE"}
    real_risk={**RISK,"stake":"4.00","stop_win":"11.00","stop_loss":"8.00"}
    response=client.post("/api/automation/configuration",
                         json={"profile":real_profile,"risk":real_risk})
    assert response.status_code==200,response.text
    assert response.json()["ready_to_start"] is False
    with sessions() as s:
        demo=s.get(db.User,uid)
        profile=s.get(db.AutomationProfile,uid)
        real=s.get(db.AccountRiskSettings,(uid,"real","CRONE"))
        assert demo.stake=="0.50" and demo.stop_loss=="0.00"
        assert profile.account_mode=="real" and profile.real_account_id=="CRONE"
        assert real.stake=="4.00" and real.stop_loss=="8.00"
        assert s.get(db.RealRobotConsent,uid) is None
        assert s.query(db.RealOrder).count()==0
    assert real_runtime.real_available() is False
    demo_broker.assert_not_awaited()
    real_broker.assert_not_awaited()


def test_bundle_invalid_real_account_never_stages_profile_or_risk(context,monkeypatch):
    client,sessions,uid,_,_=context
    class AccountsResult:
        def raise_for_status(self): pass
        def json(self):
            return {"data":{"accounts":[{"account_type":"real","account_id":"CRONE"}]}}
    class MockAccountClient:
        def __init__(self,*args,**kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return None
        async def get(self,url,headers=None): return AccountsResult()
    monkeypatch.setattr(main.httpx,"AsyncClient",MockAccountClient)
    response=client.post("/api/automation/configuration",
        json={"profile":{**PROFILE,"account_mode":"real","real_account_id":"CRFOREIGN"},
              "risk":RISK})
    assert response.status_code==403,response.text
    with sessions() as s:
        assert s.get(db.AutomationProfile,uid) is None
        assert s.query(db.AccountRiskSettings).count()==0

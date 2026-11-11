import pytest
from urllib.parse import urlparse,parse_qs
from cryptography.fernet import Fernet
from app.auth_helpers import extract_accounts, demo_id
from app.security import make_login,build_authorize_url,encrypt,decrypt,hashsecret
from app.engine import Engine,Settings

def test_pkce_unique():
    a,v=make_login();b,w=make_login()
    assert a!=b and v!=w and len(v)>=43
    url=build_authorize_url(a,v)
    q=parse_qs(urlparse(url).query)
    assert q['code_challenge_method']==['S256'] and q['state']==[a] and 'code_challenge' in q

def test_encrypt():
    t='not-a-real-token'
    enc=encrypt(t)
    assert t not in enc and decrypt(enc)==t
    assert hashsecret(t)!=t

def test_only_explicit_demo():
    assert demo_id([{'account_id':'R123','type':'real'}]) is None
    assert demo_id([{'account_id':'R123'},{'account_id':'V123','is_virtual':True}])=='V123'
    assert demo_id([{'account_id':'V777','type':'demo'}])=='V777'

def test_envelopes():
    assert extract_accounts({'data':{'accounts':[{'id':1}]}})==[{'id':1}]
    assert extract_accounts({'foo':1})==[]

def test_gales_bound_by_stoploss():
    e=Engine();e.connected=True
    e.settings=Settings.parse({'stake':'2','stop_win':'20','stop_loss':'10','martingale':True,'multiplier':'2'})
    e.pnl=e.pnl-6
    e.gale=2
    assert e.guard_reason(e.next_stake())=='Entrada ultrapassaria o Stop Loss'

def test_per_user_engine_isolation():
    a,b=Engine(),Engine()
    a.running=True
    assert b.running is False
    a.closed.append({'result':'WIN'})
    assert b.closed==[]

def test_invalid_settings():
    with pytest.raises(ValueError):Settings.parse({'stake':'1','stop_win':'10','stop_loss':'10','multiplier':'2'})

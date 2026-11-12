"""Balances must be real verified Deriv values; never construct a zero balance."""
from app.auth_helpers import real_balance,real_accounts
from pathlib import Path
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient


def test_real_balance_from_explicitly_real_account_only():
    accounts=[{'account_type':'demo','account_id':'V1','currency':'USD','balance':10000},
              {'account_type':'real','account_id':'R1','currency':'USD','balance':'154.21'}]
    assert real_balance(accounts)=={'account_id':'R1','account_type':'real',
        'currency':'USD','status':'unknown','balance':'154.21','available':True}
    assert real_balance(accounts[:1]) is None
    assert real_balance([{'account_type':'real','account_id':'R1','currency':'USD'}])['available'] is False
    assert real_balance([{'account_type':'real','account_id':'R1','currency':'USD','balance':'NaN'}])['available'] is False
    assert real_balance([{'account_type':'real','account_id':'R1','currency':'USD','balance':'-2'}])['available'] is False


def test_dashboard_always_shows_both_balances_and_live_fetch():
    html=(Path(__file__).resolve().parents[1]/'app'/'index.html').read_text()
    dashboard=html.split('<section id="dashboard"',1)[1].split('<section id="results"',1)[0]
    assert 'id="balanceMetric"' in dashboard
    assert 'id="realBalanceMetric"' in dashboard
    assert 'Saldo Treinamento' in dashboard
    assert 'Saldo Conta Real' in dashboard
    assert '/api/real/balance' in html
    assert 'loadRealBalance()' in html

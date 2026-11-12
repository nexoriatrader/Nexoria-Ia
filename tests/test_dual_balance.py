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


def test_real_balance_ws_accepts_only_official_real_endpoint():
    import pytest
    from app.real_readonly import validate_real_ws_url,RealBalanceError
    valid='wss://api.derivws.com/trading/v1/options/ws/real?otp=opaque'
    assert validate_real_ws_url(valid)==valid
    for bad in (
        'https://api.derivws.com/trading/v1/options/ws/real?otp=opaque',
        'wss://attacker.com/trading/v1/options/ws/real?otp=opaque',
        'wss://api.derivws.com/trading/v1/options/ws/demo?otp=opaque',
        'wss://api.derivws.com/trading/v1/options/ws/real?otp=',
        'wss://api.derivws.com/trading/v1/options/ws/real?otp=one&x=1',
        'wss://api.derivws.com/trading/v1/options/ws/real?otp=one&otp=two'
    ):
        with pytest.raises(RealBalanceError):
            validate_real_ws_url(bad)


def test_readonly_real_module_never_sends_buy_or_sell():
    source=(Path(__file__).resolve().parents[1]/'app'/'real_readonly.py').read_text()
    assert "'balance':1" in source
    assert "websockets.connect" in source
    assert "'buy':" not in source
    assert "'sell':" not in source
    assert "'proposal':" not in source


def test_daily_card_shows_separate_demo_and_real_results():
    html=(Path(__file__).resolve().parents[1]/'app'/'index.html').read_text()
    dashboard=html.split('<section id="dashboard"',1)[1].split('<section id="results"',1)[0]
    assert 'id="profitMetric"' in dashboard
    assert 'id="realProfitMetric"' in dashboard
    assert 'id="realProfitReason"' in dashboard
    assert 'TREINAMENTO · DEMO' in dashboard
    assert 'CONTA REAL · DERIV' in dashboard
    assert '/api/demo/results/today' in html
    assert '/api/real/results/today' in html
    assert "value.textContent='—';" in html


def test_real_profit_table_uses_only_official_settled_trades_in_brasilia_window():
    from decimal import Decimal
    from app.real_readonly import parse_real_profit_transactions, RealBalanceError
    import pytest
    start=1794452400
    transactions=[
        {'sell_time':start-1,'buy_price':'2.00','sell_price':'90.00'},
        {'sell_time':start,'buy_price':'2.00','sell_price':'3.80'},
        {'sell_time':start+40,'buy_price':'5','sell_price':'0'},
        {'sell_time':start+100,'buy_price':'2.00','sell_price':'10.00'}]
    assert parse_real_profit_transactions(transactions,start,start+60)==Decimal('-3.20')
    with pytest.raises(RealBalanceError):
        parse_real_profit_transactions([{'buy_price':2,'sell_price':3}],start,start+60)
    with pytest.raises(RealBalanceError):
        parse_real_profit_transactions([{'sell_time':start,'buy_price':'NaN','sell_price':'1'}],start,start+60)

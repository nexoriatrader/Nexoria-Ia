import asyncio
import json

import pytest

from app import demo


def test_demo_url_rejects_real_channel():
    with pytest.raises(demo.DemoTradingError):
        demo._validate_demo_url('wss://api.derivws.com/trading/v1/options/ws/real?otp=x')


def test_demo_url_accepts_only_demo_channel():
    url = 'wss://api.derivws.com/trading/v1/options/ws/demo?otp=x'
    assert demo._validate_demo_url(url) == url


class FakeSocket:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, raw):
        self.sent = json.loads(raw)

    async def recv(self):
        return json.dumps({
            'req_id': 7001,
            'proposal': {
                'id': 'proposal-demo-1', 'ask_price': 2,
                'payout': 3.8, 'currency': 'USD',
            },
        })


def test_demo_proposal_never_sends_buy(monkeypatch):
    async def fake_ws_url(token, account_id):
        return 'wss://api.derivws.com/trading/v1/options/ws/demo?otp=x'

    socket = FakeSocket()
    monkeypatch.setattr(demo, 'get_demo_ws_url', fake_ws_url)
    monkeypatch.setattr(demo.websockets, 'connect', lambda *args, **kwargs: socket)

    result = asyncio.run(demo.demo_proposal(
        'token', 'V123', symbol='1HZ100V', contract_type='CALL', stake='2.00', duration=5,
    ))

    assert result['demo_only'] is True
    assert result['bought'] is False
    assert result['proposal_id'] == 'proposal-demo-1'
    assert 'buy' not in socket.sent
    assert socket.sent['proposal'] == 1


def test_demo_buy_requires_explicit_confirmation(monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError('OTP não deveria ser chamado sem confirmação')

    monkeypatch.setattr(demo, 'get_demo_ws_url', fail_if_called)
    with pytest.raises(demo.DemoTradingError, match='Confirmação demo obrigatória'):
        asyncio.run(demo.demo_buy(
            'token', 'V123', symbol='1HZ100V', contract_type='CALL', stake='2.00',
            duration=5, confirm='YES',
        ))


class BuySocket(FakeSocket):
    def __init__(self):
        self.messages = [
            json.dumps({'req_id': 7101, 'proposal': {'id': 'proposal-demo-2', 'ask_price': 2.0}}),
            json.dumps({'req_id': 7102, 'buy': {'contract_id': 123456, 'buy_price': 2.0}}),
        ]

    async def recv(self):
        return self.messages.pop(0)


def test_demo_buy_uses_demo_channel_and_returns_contract(monkeypatch):
    async def fake_ws_url(token, account_id):
        return 'wss://api.derivws.com/trading/v1/options/ws/demo?otp=x'

    socket = BuySocket()
    monkeypatch.setattr(demo, 'get_demo_ws_url', fake_ws_url)
    monkeypatch.setattr(demo.websockets, 'connect', lambda *args, **kwargs: socket)

    result = asyncio.run(demo.demo_buy(
        'token', 'V123', symbol='1HZ100V', contract_type='CALL', stake='2.00',
        duration=5, confirm='DEMO_ONLY',
    ))

    assert result == {
        'contract_id': '123456', 'buy_price': '2.0', 'symbol': '1HZ100V',
        'contract_type': 'CALL', 'duration_minutes': 5, 'currency': 'USD',
        'demo_only': True, 'bought': True,
    }
    assert socket.sent['buy'] == 'proposal-demo-2'

import asyncio
import json

import pytest

from app import demo


DEMO_URL = "wss://api.derivws.com/trading/v1/options/ws/demo?otp=x"


def test_demo_url_rejects_real_channel():
    with pytest.raises(demo.DemoTradingError):
        demo._validate_demo_url("wss://api.derivws.com/trading/v1/options/ws/real?otp=x")


@pytest.mark.parametrize("url", [
    "wss://api.derivws.com/trading/v1/options/ws/demo",
    "wss://api.derivws.com/trading/v1/options/ws/demo?otp=",
    "wss://api.derivws.com/trading/v1/options/ws/demo?otp=x&otp=y",
    "wss://api.derivws.com/trading/v1/options/ws/demo?otp=x&other=y",
])
def test_demo_url_rejects_missing_or_ambiguous_otp(url):
    with pytest.raises(demo.DemoTradingError):
        demo._validate_demo_url(url)


def test_demo_url_accepts_exact_official_demo_channel():
    assert demo._validate_demo_url(DEMO_URL) == DEMO_URL


class FakeSocket:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        return json.dumps(self.responses.pop(0))


class BuySocket(FakeSocket):
    def __init__(self):
        super().__init__([
            {"req_id": 7100, "balance": {"balance": 1000, "currency": "EUR"}},
            {"req_id": 7101, "proposal": {"id": "proposal-demo-2", "ask_price": 2.0}},
            {"req_id": 7102, "buy": {"contract_id": 123456, "buy_price": 2.0}},
        ])


def test_demo_account_balance_is_read_only(monkeypatch):
    async def fake_ws_url(token, account_id):
        return DEMO_URL

    socket = FakeSocket([
        {"req_id": 7301, "balance": {"balance": 987.65, "currency": "EUR"}},
    ])
    monkeypatch.setattr(demo, "get_demo_ws_url", fake_ws_url)
    monkeypatch.setattr(demo.websockets, "connect", lambda *args, **kwargs: socket)

    result = asyncio.run(demo.demo_account_balance("token", "V123"))

    assert result == {"balance": "987.65", "currency": "EUR", "demo_only": True}
    assert socket.sent == [{"balance": 1, "req_id": 7301}]


def test_demo_proposal_never_sends_buy(monkeypatch):
    async def fake_ws_url(token, account_id):
        return DEMO_URL

    socket = FakeSocket([
        {"req_id": 7000, "balance": {"balance": 1000, "currency": "EUR"}},
        {"req_id": 7001, "proposal": {
            "id": "proposal-demo-1", "ask_price": 2, "payout": 3.8, "currency": "EUR",
        }},
    ])
    monkeypatch.setattr(demo, "get_demo_ws_url", fake_ws_url)
    monkeypatch.setattr(demo.websockets, "connect", lambda *args, **kwargs: socket)

    result = asyncio.run(demo.demo_proposal(
        "token", "V123", symbol="1HZ100V", contract_type="CALL", stake="2.00", duration=5,
    ))

    assert result["demo_only"] is True
    assert result["bought"] is False
    assert result["proposal_id"] == "proposal-demo-1"
    assert result["currency"] == "EUR"
    assert socket.sent[0] == {"balance": 1, "req_id": 7000}
    assert socket.sent[1]["proposal"] == 1
    assert "subscribe" not in socket.sent[1]  # preflight stays one-off and read-only
    assert all("buy" not in request for request in socket.sent)


def test_demo_buy_requires_explicit_confirmation(monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("OTP não deveria ser chamado sem confirmação")

    monkeypatch.setattr(demo, "get_demo_ws_url", fail_if_called)
    with pytest.raises(demo.DemoTradingError, match="Confirmação demo obrigatória"):
        asyncio.run(demo.demo_buy(
            "token", "V123", symbol="1HZ100V", contract_type="CALL", stake="2.00",
            duration=5, confirm="YES",
        ))


def test_demo_buy_uses_demo_channel_and_returns_contract(monkeypatch):
    async def fake_ws_url(token, account_id):
        return DEMO_URL

    socket = BuySocket()
    monkeypatch.setattr(demo, "get_demo_ws_url", fake_ws_url)
    monkeypatch.setattr(demo.websockets, "connect", lambda *args, **kwargs: socket)

    result = asyncio.run(demo.demo_buy(
        "token", "V123", symbol="1HZ100V", contract_type="CALL", stake="2.00",
        duration=5, confirm="DEMO_ONLY",
    ))

    assert result == {
        "contract_id": "123456", "buy_price": "2.0", "symbol": "1HZ100V",
        "contract_type": "CALL", "duration_minutes": 5, "currency": "EUR",
        "demo_only": True, "bought": True,
    }
    assert socket.sent[0] == {"balance": 1, "req_id": 7100}
    assert socket.sent[1]["proposal"] == 1
    assert socket.sent[1]["currency"] == "EUR"
    assert socket.sent[1]["subscribe"] == 1  # required for new Deriv buy API
    assert socket.sent[2]["buy"] == "proposal-demo-2"


def test_deriv_proposal_error_code_is_sanitized():
    socket = FakeSocket([
        {"req_id": 7001, "error": {"code": "ContractUnavailable", "message": "No contract"}},
    ])
    with pytest.raises(demo.DemoTradingError) as err:
        asyncio.run(demo._response_for(socket, 7001))
    assert err.value.deriv_code == "ContractUnavailable"


def test_deriv_error_code_does_not_allow_sensitive_or_untrusted_text():
    socket = FakeSocket([
        {"req_id": 7001, "error": {"code": "secret=token\\n", "message": "Denied"}},
    ])
    with pytest.raises(demo.DemoTradingError) as err:
        asyncio.run(demo._response_for(socket, 7001))
    assert err.value.deriv_code == "UNSPECIFIED"

"""The live-money adapter is isolated and fail-closed; no real orders in tests."""
import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from app import real_broker as real


def test_real_otp_cannot_point_at_demo_or_external_server():
    assert real.validate_real_ws_url(
        "wss://api.derivws.com/trading/v1/options/ws/real?otp=once"
    ).endswith("?otp=once")
    for url in (
        "wss://api.derivws.com/trading/v1/options/ws/demo?otp=once",
        "wss://not-deriv.example/trading/v1/options/ws/real?otp=once",
        "ws://api.derivws.com/trading/v1/options/ws/real?otp=once",
        "wss://api.derivws.com/trading/v1/options/ws/real?otp=",
        "wss://api.derivws.com/trading/v1/options/ws/real?otp=x&token=y",
    ):
        with pytest.raises(real.RealExecutionError):
            real.validate_real_ws_url(url)


def test_real_validation_blocks_invalid_trade_without_network(monkeypatch):
    token="test-token"
    get_url=AsyncMock(side_effect=AssertionError("should not open broker channel"))
    monkeypatch.setattr(real, "get_real_ws_url", get_url)
    async def check():
        with pytest.raises(real.RealExecutionError):
            await real.buy_real_contract(token,"REAL1",symbol="R_100",
                    contract_type="CALL",stake="0.01",duration=5,
                    currency="USD",min_payout_ratio="1.85")
        with pytest.raises(real.RealExecutionError):
            await real.buy_real_contract(token,"REAL1",symbol="R_100",
                    contract_type="CALL",stake="1",duration=7,
                    currency="USD",min_payout_ratio="1.85")
        get_url.assert_not_awaited()
    asyncio.run(check())


class DummyWebsocket:
    def __init__(self, ask="1.0", payout="1.9", error_after_buy=False):
        self.sent=[]
        self.ask=ask
        self.payout=payout
        self.error_after_buy=error_after_buy

    async def send(self, msg):
        self.sent.append(json.loads(msg))

    async def recv(self):
        request=self.sent[-1]
        if request["req_id"]==8402:
            return json.dumps({"req_id":8402,"proposal":{
                "id":"proposal-test","ask_price":self.ask,"payout":self.payout
            }})
        if self.error_after_buy:
            raise ConnectionError("simulated unknown buy result")
        return json.dumps({"req_id":8403,"buy":{
            "contract_id":123456,"buy_price":self.ask,"purchase_time":12345678
        }})


def broker_mock(monkeypatch, socket):
    monkeypatch.setattr(real,"get_real_ws_url",
                        AsyncMock(return_value="wss://api.derivws.com/trading/v1/options/ws/real?otp=test"))
    @asynccontextmanager
    async def open_ws(*args,**kwargs):
        yield socket
    monkeypatch.setattr(real.websockets,"connect",open_ws)


def test_real_buy_blocks_bad_payout_before_any_purchase(monkeypatch):
    socket=DummyWebsocket(payout="1.1")
    broker_mock(monkeypatch,socket)
    async def check():
        with pytest.raises(real.RealExecutionError):
            await real.buy_real_contract("token","REAL123",symbol="R_100",
                contract_type="CALL",stake="1",duration=5,
                currency="USD",min_payout_ratio="1.85")
        assert len(socket.sent)==1
        assert socket.sent[0]["proposal"]==1
    asyncio.run(check())


def test_real_buy_returns_account_scoped_receipt(monkeypatch):
    socket=DummyWebsocket()
    broker_mock(monkeypatch,socket)
    async def check():
        result=await real.buy_real_contract("token","REAL123",symbol="R_100",
            contract_type="CALL",stake="1",duration=5,
            currency="USD",min_payout_ratio="1.85")
        assert result["account_mode"]=="real"
        assert result["contract_id"]=="123456"
        assert len(socket.sent)==2
        assert socket.sent[1]["buy"]=="proposal-test"
    asyncio.run(check())


def test_real_buy_unknown_result_is_not_safe_to_retry(monkeypatch):
    socket=DummyWebsocket(error_after_buy=True)
    broker_mock(monkeypatch,socket)
    async def check():
        with pytest.raises(real.RealPurchaseUncertain):
            await real.buy_real_contract("token","REAL123",symbol="R_100",
                contract_type="PUT",stake="1",duration=5,
                currency="USD",min_payout_ratio="1.85")
        assert len(socket.sent)==2
    asyncio.run(check())

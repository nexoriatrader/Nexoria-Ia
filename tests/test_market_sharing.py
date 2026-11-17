"""Regression tests for shared Deriv public candles and five curated markets."""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from app import market, automation_markets


def _candles():
    return {"candles":[{"epoch":int(time.time()//60)*60-60,
                        "open":"1","high":"1.1","low":"0.9","close":"1"}]}


def test_shared_candle_queries_are_deduplicated_across_robots(monkeypatch):
    async def check():
        market._candle_snapshots.clear()
        market._candle_inflight.clear()
        calls=[]
        async def mock_request(payload,*,expected,timeout):
            calls.append(dict(payload))
            await asyncio.sleep(.03)
            return _candles()
        monkeypatch.setattr(market,"public_request",mock_request)
        first,second,third=await asyncio.gather(
            market.shared_candle_request("R_100",60,165),
            market.shared_candle_request("R_100",60,165),
            market.shared_candle_request("R_100",60,165))
        assert first==second==third
        assert len(calls)==1
        await market.shared_candle_request("R_100",60,165)
        assert len(calls)==1
        await market.shared_candle_request("R_75",60,165)
        assert len(calls)==2
        await market.shared_candle_request("R_100",300,165)
        assert len(calls)==3
        market._candle_snapshots.clear()
        market._candle_inflight.clear()
    asyncio.run(check())


def test_shared_candle_cache_rolls_on_new_candle(monkeypatch):
    async def check():
        market._candle_snapshots.clear()
        market._candle_inflight.clear()
        clock=[120.1]
        monkeypatch.setattr(time,"time",lambda:clock[0])
        calls=AsyncMock(return_value=_candles())
        monkeypatch.setattr(market,"public_request",calls)
        await market.shared_candle_request("R_25",60,165)
        clock[0]=179.9
        await market.shared_candle_request("R_25",60,165)
        assert calls.await_count==1
        clock[0]=180.1
        await market.shared_candle_request("R_25",60,165)
        assert calls.await_count==2
        market._candle_snapshots.clear()
        market._candle_inflight.clear()
    asyncio.run(check())


def test_shared_market_errors_never_cached(monkeypatch):
    async def check():
        market._candle_snapshots.clear()
        market._candle_inflight.clear()
        call=AsyncMock(side_effect=[
            market.MarketError("Deriv offline"),_candles()])
        monkeypatch.setattr(market,"public_request",call)
        with pytest.raises(market.MarketError):
            await market.shared_candle_request("R_50",60,165)
        result=await market.shared_candle_request("R_50",60,165)
        assert result["candles"]
        assert call.await_count==2
        market._candle_snapshots.clear()
        market._candle_inflight.clear()
    asyncio.run(check())


def test_featured_catalogue_five_only_from_live_broker(monkeypatch):
    async def check():
        rows=[
            {"symbol":"frxUSDCAD","name":"USD/CAD","group":"forex"},
            {"symbol":"frxGBPUSD","name":"GBP/USD","group":"forex"},
            {"symbol":"frxEURUSD","name":"EUR/USD","group":"forex"},
            {"symbol":"frxUSDJPY","name":"USD/JPY","group":"forex"},
            {"symbol":"frxAUDUSD","name":"AUD/USD","group":"forex"},
            {"symbol":"frxUSDCHF","name":"USD/CHF","group":"forex"},
        ]
        monkeypatch.setattr(automation_markets,"instruments_by_group",
                            AsyncMock(return_value=rows))
        selected=await automation_markets.featured_instruments_by_group("forex")
        assert len(selected)==5
        assert [x["symbol"] for x in selected]==[
            "frxEURUSD","frxGBPUSD","frxUSDJPY","frxAUDUSD","frxUSDCAD"]
        # No fabricated fifth instrument if Deriv returned fewer than five.
        automation_markets.instruments_by_group.return_value=rows[:2]
        assert len(await automation_markets.featured_instruments_by_group("forex"))==2
    asyncio.run(check())


def test_missing_newly_closed_bar_is_not_cached(monkeypatch):
    async def check():
        market._candle_snapshots.clear()
        market._candle_inflight.clear()
        clock=[180.5]
        monkeypatch.setattr(time,"time",lambda:clock[0])
        stale={"candles":[{"epoch":60,"open":"1","close":"1","high":"1.1","low":"0.9"}]}
        fresh={"candles":[{"epoch":120,"open":"1","close":"1","high":"1.1","low":"0.9"}]}
        calls=AsyncMock(side_effect=[stale,fresh])
        monkeypatch.setattr(market,"public_request",calls)
        assert await market.shared_candle_request("R_100",60,165)==stale
        assert await market.shared_candle_request("R_100",60,165)==fresh
        assert calls.await_count==2
        assert await market.shared_candle_request("R_100",60,165)==fresh
        assert calls.await_count==2
        market._candle_snapshots.clear()
        market._candle_inflight.clear()
    asyncio.run(check())

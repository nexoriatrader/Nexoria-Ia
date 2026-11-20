"""I02 integration tests: synthetic public research, never Deriv trading.

The existing P0/F01 ledgers and broker are never mutated or contacted by I02.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import asset_scanner, automation_markets, db, main, market, real_broker
from app.intelligence2 import market_scanner as scan
from app.security import encrypt, hashsecret

NOW = 1_800_000_020
LATEST = (NOW // 60 - 1) * 60


def _asset(symbol, group="forex", *, open=True, suspended=False):
    return {
        "symbol": symbol, "group": group, "name": "Example " + symbol,
        "catalogue_open": open, "catalogue_suspended": suspended,
    }


def _bars(*, last=LATEST, length=96, flat=False):
    candles = []
    for i in range(length):
        close = 100.0 if flat else 100.0 + i * 0.08 + (i % 3) * 0.06
        candles.append({
            "epoch": last - (length - 1 - i) * 60,
            "open": close - 0.02, "close": close,
            "high": close + 0.1, "low": close - 0.1,
        })
    return {"candles": candles}


def _synthetic_assessment(asset, *, score=80.0):
    return {
        "symbol": asset["symbol"], "market": asset["group"],
        "name": asset["name"], "score": score,
        "eligible": True, "reason_codes": ["SYNTHETIC_RESEARCH_ONLY"],
        "status": "RESEARCH_ELIGIBLE", "execution_allowed": False,
    }


def test_live_broker_catalogue_covers_known_and_new_markets(monkeypatch):
    async def run():
        automation_markets._catalog_cache.update(at=0.0, items=[])
        response = {"active_symbols": [
            {"symbol": "frxEURUSD", "market": "forex",
             "underlying_symbol_name": "EUR/USD", "exchange_is_open": 1},
            {"symbol": "CRYPTO1", "market": "cryptocurrency",
             "underlying_symbol_name": "Crypto 1", "exchange_is_open": 0},
            {"symbol": "FUTURE1", "market": "new-market",
             "underlying_symbol_name": "Future", "is_trading_suspended": 1},
            {"symbol": "RAW001", "underlying_symbol_name": "Unknown market"},
        ]}
        public = AsyncMock(return_value=response)
        monkeypatch.setattr(automation_markets, "public_request", public)
        records = await automation_markets.market_instruments(force=True)
        assert {x["symbol"]: x["group"] for x in records} == {
            "frxEURUSD": "forex", "CRYPTO1": "deriv_cryptocurrency",
            "FUTURE1": "deriv_new_market", "RAW001": "unclassified",
        }
        assert next(r for r in records if r["symbol"] == "CRYPTO1")["catalogue_open"] is False
        assert next(r for r in records if r["symbol"] == "FUTURE1")["catalogue_suspended"] is True
        # Existing catalogue cache is reused: no new public request.
        await automation_markets.market_instruments()
        assert public.await_count == 1
        automation_markets._catalog_cache.update(at=0.0, items=[])
    asyncio.run(run())


def test_reuses_i01_closed_candles_and_public_contract_types(monkeypatch):
    async def run():
        monkeypatch.setattr(automation_markets, "market_instruments",
                            AsyncMock(return_value=[_asset("R_100", "derived")]))
        candles = AsyncMock(return_value=_bars())
        monkeypatch.setattr(market, "shared_candle_request", candles)
        contracts = AsyncMock(return_value=["CALL", "PUT"])
        monkeypatch.setattr(scan, "discover_contracts", contracts)
        report = await asset_scanner.scan_intelligence_markets(now=NOW)
        assert report["state"] == "RESEARCH_ONLY"
        assert report["selected_total"] == 1
        item = report["markets"]["derived"]["selected"][0]
        assert item["symbol"] == "R_100"
        assert item["candle_quality"] == "COMPLETE"
        assert item["execution_allowed"] is False
        assert item["execution_conditions"]["contract_duration_verified"] is False
        assert item["context_inputs"]["data_quality"] == "COMPLETE"
        assert item["timeframe_inputs"]["granularity_seconds"] == 60
        assert 0 <= item["score"] <= 100
        assert set(item["score_components"]) == {
            "closed_candle_quality", "freshness", "relative_volatility",
            "trend_structure", "existing_strategy_family",
            "public_contract_conditions",
        }
        candles.assert_awaited_once_with("R_100", 60, 96, timeout=8.0)
        contracts.assert_awaited_once_with("R_100")
    asyncio.run(run())


def test_all_markets_three_primary_two_light_reserves_and_explainable_sort(monkeypatch):
    groups = ["forex", "derived", "stocks_indices", "commodities", "deriv_cryptocurrency"]
    catalogue = [_asset(f"{group.upper()}{i}", group) for group in groups for i in range(1, 7)]
    monkeypatch.setattr(automation_markets, "market_instruments",
                        AsyncMock(return_value=catalogue))
    calls = []
    async def fake_inspect(asset, *, observed_at, semaphore):
        async with semaphore:
            calls.append(asset["symbol"])
            i = int(asset["symbol"][-1])
            return _synthetic_assessment(asset, score=60 + i)
    monkeypatch.setattr(scan, "_inspect", fake_inspect)
    report = asyncio.run(scan.scan_market_opportunities(now=NOW))
    assert report["catalogue_markets"] == 5
    assert report["catalogue_assets"] == 30
    assert report["inspected_assets"] == 30
    assert len(calls) == 30
    assert report["scan_coverage_complete"] is True
    for group in groups:
        state = report["markets"][group]
        assert len(state["selected"]) == 3
        assert len(state["reserves"]) == 2
        assert [x["symbol"][-1] for x in state["selected"]] == ["6", "5", "4"]
        assert [x["symbol"][-1] for x in state["reserves"]] == ["3", "2"]
        assert all(x["monitoring"] == "RECHECK_ON_NEXT_SCAN" for x in state["reserves"])
        assert len(state["assessments"]) == 6
    assert report["selected_total"] == 15
    assert report["orders_sent"] == 0
    assert report["execution_allowed"] is False


def test_dynamic_change_and_reserves_remain_in_light_followup(monkeypatch):
    catalogue = [_asset("F" + str(i)) for i in range(1, 14)]
    monkeypatch.setattr(automation_markets, "market_instruments",
                        AsyncMock(return_value=catalogue))
    bump = {"F1": 100, "F2": 99, "F3": 98, "F4": 97, "F5": 96}
    inspected = []
    async def fake_inspect(asset, *, observed_at, semaphore):
        inspected.append(asset["symbol"])
        return _synthetic_assessment(asset, score=bump.get(asset["symbol"], 20))
    monkeypatch.setattr(scan, "_inspect", fake_inspect)
    before = asyncio.run(scan.scan_market_opportunities(now=NOW))
    prior = before["markets"]["forex"]
    assert prior["coverage_complete"] is False
    primary = {x["symbol"] for x in prior["selected"]}
    reserves = {x["symbol"] for x in prior["reserves"]}
    bump.update({"F2": 5, "F4": 110})
    inspected.clear()
    after = asyncio.run(scan.scan_market_opportunities(previous=before, now=NOW + 300))
    # Every former primary and reserve is *rechecked*, even with rotation.
    assert primary | reserves <= set(inspected)
    changes = after["markets"]["forex"]["selection_changes"]
    assert "F4" in changes["added"]
    assert changes["removed"]
    assert after["markets"]["forex"]["coverage_complete"] is False
    assert all(row["reason_codes"] == ["DEFERRED_CAPACITY"]
               for row in after["markets"]["forex"]["assessments"]
               if row["status"] == "EXCLUDED" and row["symbol"] not in inspected)


def test_ranking_rotates_bounded_sample_without_misrepresenting_coverage(monkeypatch):
    catalogue = [_asset("F" + str(i).zfill(2)) for i in range(21)]
    monkeypatch.setattr(automation_markets, "market_instruments",
                        AsyncMock(return_value=catalogue))
    examined = []
    async def fake_inspect(asset, *, observed_at, semaphore):
        examined.append(asset["symbol"])
        return _synthetic_assessment(asset, score=80)
    monkeypatch.setattr(scan, "_inspect", fake_inspect)
    first = asyncio.run(scan.scan_market_opportunities(now=NOW))
    first_examined = set(examined)
    examined.clear()
    second = asyncio.run(scan.scan_market_opportunities(now=NOW + 300))
    assert len(first_examined) == 8
    assert len(examined) == 8
    assert set(examined) != first_examined
    for report in (first, second):
        m = report["markets"]["forex"]
        assert m["deferred_assets"] == 13
        assert m["catalogue_assets"] == 21
        assert m["coverage_complete"] is False
        assert report["selection_scope"] == "EVALUATED_SAMPLE_NOT_GLOBAL_MARKET_RANKING"
        assert len(m["selected"]) <= 3 and len(m["reserves"]) <= 2


def test_bad_candles_closed_session_and_missing_contracts_never_select(monkeypatch):
    assets = [
        _asset("BADGAP"), _asset("BADSTALE"), _asset("BADFLAT"),
        _asset("BADSHORT"), _asset("NOCP"), _asset("CLOSED", open=False),
        _asset("SUSPEND", suspended=True), _asset("UNKNOWN", group="unclassified"),
    ]
    monkeypatch.setattr(automation_markets, "market_instruments",
                        AsyncMock(return_value=assets))
    windows = {
        "BADGAP": {"quality": "GAPPED", "usable_for_research": False, "missing_intervals": 2},
        "BADSTALE": {"quality": "STALE", "usable_for_research": False, "missing_intervals": 0},
    }
    async def read(symbol, granularity, count, *, now, timeout):
        if symbol in windows:
            return windows[symbol]
        raw = _bars(flat=symbol == "BADFLAT",
                    length=40 if symbol == "BADSHORT" else 96)
        from app.trade_selector_v2 import strict_closed_bars
        candles = strict_closed_bars(raw, 60, now=now)
        return {"quality": "COMPLETE", "usable_for_research": True,
                "missing_intervals": 0, "closed_count": len(candles),
                "candles": candles, "last_closed_epoch": candles[-1]["epoch"]}
    monkeypatch.setattr(scan, "read_closed_window", read)
    contracts = AsyncMock(return_value=["CALL"])
    monkeypatch.setattr(scan, "discover_contracts", contracts)
    report = asyncio.run(scan.scan_market_opportunities(now=NOW))
    assert report["state"] == "NO_OPPORTUNITIES"
    assert report["selected_total"] == 0
    rejects = {a["symbol"]: a["reason_codes"][0]
               for m in report["markets"].values() for a in m["assessments"]}
    assert rejects["BADGAP"] == "CANDLE_GAPPED"
    assert rejects["BADSTALE"] == "CANDLE_STALE"
    assert rejects["BADFLAT"] == "FLAT_MARKET"
    assert rejects["BADSHORT"] == "INSUFFICIENT_HISTORY"
    assert rejects["NOCP"] == "CALL_PUT_UNAVAILABLE"
    assert rejects["CLOSED"] == "SESSION_CLOSED"
    assert rejects["SUSPEND"] == "TRADING_SUSPENDED"
    assert rejects["UNKNOWN"] == "MARKET_UNCLASSIFIED"
    assert contracts.await_count == 1


def test_network_errors_and_empty_catalogue_do_not_fabricate_opportunities(monkeypatch):
    async def run():
        monkeypatch.setattr(automation_markets, "market_instruments",
                            AsyncMock(return_value=[]))
        assert (await scan.scan_market_opportunities(now=NOW))["state"] == "NO_OPPORTUNITIES"
        monkeypatch.setattr(automation_markets, "market_instruments",
                            AsyncMock(return_value=[_asset("R_100")]))
        monkeypatch.setattr(scan, "read_closed_window",
                            AsyncMock(side_effect=market.MarketError("offline")))
        report = await scan.scan_market_opportunities(now=NOW)
        assert report["state"] == "NO_OPPORTUNITIES"
        assert report["markets"]["forex"]["assessments"][0]["reason_codes"] == [
            "PUBLIC_RESEARCH_UNAVAILABLE"
        ]
    asyncio.run(run())


def test_concurrency_is_bounded_and_no_real_broker_calls(monkeypatch):
    assets = [_asset("F" + str(i), "forex") for i in range(8)] + [
        _asset("D" + str(i), "derived") for i in range(8)
    ]
    monkeypatch.setattr(automation_markets, "market_instruments",
                        AsyncMock(return_value=assets))
    current = peak = 0
    async def slow_read(symbol, granularity, count, *, now, timeout):
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        try:
            await asyncio.sleep(0.004)
            return {"usable_for_research": True, "quality": "COMPLETE",
                    "closed_count": 96, "candles": _bars()["candles"],
                    "last_closed_epoch": LATEST}
        finally:
            current -= 1
    monkeypatch.setattr(scan, "read_closed_window", slow_read)
    contracts = AsyncMock(return_value=["CALL", "PUT"])
    monkeypatch.setattr(scan, "discover_contracts", contracts)
    buy = AsyncMock(side_effect=AssertionError("BUY forbidden by I02"))
    monkeypatch.setattr(real_broker, "buy_real_contract", buy)
    report = asyncio.run(scan.scan_market_opportunities(now=NOW))
    assert 1 < peak <= scan.MAX_PARALLEL_READS
    assert contracts.await_count == 16
    assert report["inspected_assets"] == 16
    assert report["orders_sent"] == 0 and report["execution_allowed"] is False
    buy.assert_not_awaited()


def test_existing_authenticated_route_preserves_open_orders_and_tenant_isolation(
    monkeypatch, tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path}/intelligence_i02.sqlite")
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setenv("NEXORA_INTELLIGENCE_READONLY_ENABLED", "true")
    monkeypatch.setenv("NEXORA_REAL_AUTOPILOT_ENABLED", "false")
    monkeypatch.setenv("NEXORA_REAL_RELEASE_APPROVED", "false")
    with factory() as sess:
        a = db.User(deriv_account_id="V_I02_A", selected_mode="demo")
        b = db.User(deriv_account_id="V_I02_B", selected_mode="demo")
        sess.add_all([a, b])
        sess.flush()
        ua, ub = a.id, b.id
        for uid, sid in ((ua, "i02-a"), (ub, "i02-b")):
            sess.add(db.Session(session_hash=hashsecret(sid),
                user_id=uid,token_encrypted=encrypt("synthetic-token"),
                expires_at=db.utcnow()+timedelta(hours=1)))
        sess.add_all([
            db.DemoOrder(user_id=ua,request_key="i02-demo-a",active_slot=ua,
                         state="UNKNOWN",symbol="OLD_DEMO",direction="CALL",
                         duration_minutes=1,stake="1.00"),
            db.RealOrder(user_id=ua,account_id="CR_I02_A",
                         request_key="i02-real-a",active_slot="CR_I02_A",
                         state="CONFIRMED",symbol="OLD_REAL",direction="PUT",
                         duration_minutes=5,stake="2.00",currency="USD",
                         contract_id="1234567"),
            db.RealOrder(user_id=ub,account_id="CR_I02_B",
                         request_key="i02-real-b",active_slot="CR_I02_B",
                         state="PENDING",symbol="OTHER_REAL",direction="CALL",
                         duration_minutes=1,stake="2.00",currency="USD"),
        ])
        sess.commit()

    sample = {"version":"intelligence2_i02_v1","state":"RESEARCH_ONLY",
              "markets":{"forex":{"selected":[{"symbol":"NEW_FX"}],
                                  "reserves":[]}},
              "orders_sent":0,"execution_allowed":False}
    scanner = AsyncMock(return_value=sample)
    monkeypatch.setattr(main, "scan_intelligence_markets", scanner)
    monkeypatch.setattr(main, "scan_assets", AsyncMock(return_value={
        "state":"RESEARCH_ONLY", "items":[],"orders_sent":0
    }))
    monkeypatch.setattr(main, "_asset_scanner_research_cache", {})
    monkeypatch.setattr(main, "_asset_scanner_cache", None)
    monkeypatch.setattr(main, "_asset_scanner_cache_time", 0.0)
    monkeypatch.setattr(main, "_asset_scanner_cache_version", 1)
    monkeypatch.setattr(main, "_asset_scanner_last_v2", None)
    client = TestClient(main.app)
    try:
        assert client.get("/api/research/asset-scanner?version=2").status_code == 401
        client.cookies.set("nexora_sid", "i02-a")
        first = client.get("/api/research/asset-scanner?version=2")
        assert first.status_code == 200
        data = first.json()
        assert data["cached"] is False
        assert data["contract_continuity"] == "READ_ONLY_INDEPENDENT_OF_SELECTION"
        assert {x["symbol"] for x in data["preserved_open_contracts"]} == {
            "OLD_REAL", "OLD_DEMO",
        }
        assert {x["account_mode"] for x in data["preserved_open_contracts"]} == {
            "demo", "real",
        }
        assert data["markets"]["forex"]["selected"][0]["symbol"] == "NEW_FX"
        assert client.get("/api/research/asset-scanner?version=2").json()["cached"] is True
        scanner.assert_awaited_once()
        client.cookies.set("nexora_sid", "i02-b")
        other = client.get("/api/research/asset-scanner?version=2").json()
        assert {x["symbol"] for x in other["preserved_open_contracts"]} == {
            "OTHER_REAL",
        }
        client.cookies.set("nexora_sid", "i02-a")
        assert client.get("/api/research/asset-scanner?version=9").status_code == 422
        original = client.get("/api/research/asset-scanner")
        assert original.status_code == 200
        assert original.json()["items"] == []
        with factory() as sess:
            assert sess.scalar(select(db.DemoOrder.state).where(
                db.DemoOrder.user_id==ua)) == "UNKNOWN"
            assert sess.scalar(select(db.RealOrder.state).where(
                db.RealOrder.user_id==ua)) == "CONFIRMED"
    finally:
        client.close()
        engine.dispose()

"""Synthetic I03 tests: deterministic context, hierarchy and fail-closed gates.

No real broker requests, no production DB, no ML training, no financial writes.
"""
from __future__ import annotations

import asyncio
import math
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import asset_scanner, db, main, market, real_broker
from app.intelligence2 import context_engine as context
from app.intelligence2 import market_data, timeframe_engine as tf
from app.security import encrypt, hashsecret

NOW = 1_800_000_020


def _bars(seconds=60, *, mode="up", count=96):
    last = (NOW // seconds - 1) * seconds
    rows = []
    for i in range(count):
        if mode in ("up", "irregular"):
            close = 100 + i * .18
        elif mode == "down":
            close = 130 - i * .18
        elif mode == "flat":
            close = 100.0
        elif mode == "range":
            close = 100 + math.sin(i * .7)
        elif mode == "transition":
            close = 100 + min(i, 85) * .18 - max(0, i-85) * .9
        elif mode == "high_volatility":
            close = 100 + i * .18 + (3 if i % 2 else -3)
        else:
            raise ValueError(mode)
        op = close - .06 if i % 2 else close + .06
        hi = max(close, op) + .12
        lo = min(close, op) - .12
        if mode == "irregular" and i == count - 1:
            hi, lo = close * 1.3, close * .7
        rows.append({
            "epoch": last - (count - 1 - i) * seconds,
            "open": op, "high": hi, "low": lo, "close": close,
        })
    return {"candles": rows}


def _window(seconds=60, *, mode="up", count=96):
    bars = _bars(seconds, mode=mode, count=count)["candles"]
    return {
        "quality": "COMPLETE", "usable_for_research": True,
        "last_closed_epoch": bars[-1]["epoch"], "closed_count": len(bars),
        "candles": bars, "observed_at_epoch": NOW,
        "granularity_seconds": seconds, "read_only": True,
        "execution_allowed": False,
    }


def _context(mode="up", seconds=60, **overrides):
    window = _window(seconds, mode=mode)
    return context.analyze_context(window, market="forex", symbol="F1",
                                   timeframe_seconds=seconds,
                                   observed_at=NOW) | overrides


def _candidate(symbol="F1", market_name="forex", *, vol=.001,
               family="generic", regime="TRENDING"):
    return {
        "symbol": symbol, "name": symbol, "market": market_name,
        "eligible": True, "execution_allowed": False,
        "realized_volatility": vol,
        "strategy_family": family, "market_regime": regime,
        "candle_quality": "COMPLETE",
        "last_closed_epoch": (NOW//60-1)*60,
    }


def _ctx(direction, *, strength=.80, stability=.90, confidence=.90,
         state="UPTREND", confirm=True, usable=True):
    return {
        "state": state, "direction": direction,
        "research_usable": usable, "strength": strength,
        "stability": stability, "confidence": confidence,
        "evidence": {"two_closed_candles_confirm_direction": confirm},
        "execution_allowed": False,
    }


def test_context_identifies_up_and_down_trends_with_existing_indicators():
    up = _context("up")
    down = _context("down")
    assert up["state"] == "UPTREND" and up["direction"] == "UP"
    assert down["state"] == "DOWNTREND" and down["direction"] == "DOWN"
    for c in (up, down):
        assert 0 <= c["strength"] <= 1
        assert 0 <= c["stability"] <= 1
        assert 0 <= c["confidence"] <= .95
        assert c["confidence_kind"] == "HEURISTIC_NOT_PROBABILISTIC"
        assert c["evidence"]["closed_bars_used"] == context.MIN_BARS
        assert "ema20" in c["evidence"] and "rsi14" in c["evidence"]
        assert c["ml"]["used"] is False
        assert c["execution_allowed"] is False


def test_context_classifies_flat_ranging_transition_high_volatility_irregular():
    assert _context("flat")["state"] == "LOW_VOLATILITY"
    assert _context("range")["state"] == "RANGING"
    assert _context("transition")["state"] == "TRANSITION"
    assert _context("high_volatility")["state"] == "HIGH_VOLATILITY"
    irregular = _context("irregular")
    assert irregular["state"] == "IRREGULAR"
    assert irregular["research_usable"] is False
    assert irregular["execution_allowed"] is False


@pytest.mark.parametrize("damage,code", [
    ("stale", "INVALID_OR_STALE_I01_WINDOW"),
    ("gapped", "INVALID_OR_STALE_I01_WINDOW"),
    ("short", "INSUFFICIENT_OR_MISALIGNED_BARS"),
    ("misaligned", "INSUFFICIENT_OR_MISALIGNED_BARS"),
    ("hole", "INCONSISTENT_CONTEXT_INPUT"),
    ("nan", "INCONSISTENT_CONTEXT_INPUT"),
    ("open", "INSUFFICIENT_OR_MISALIGNED_BARS"),
])
def test_context_indeterminate_for_untrusted_data(damage, code):
    w = _window()
    if damage == "stale":
        w["quality"] = "STALE"
    elif damage == "gapped":
        w["quality"] = "GAPPED"
    elif damage == "short":
        w = _window(count=50)
    elif damage == "misaligned":
        w["last_closed_epoch"] -= 60
    elif damage == "hole":
        w["candles"][-3]["epoch"] -= 60
    elif damage == "nan":
        w["candles"][-1]["close"] = float("nan")
    elif damage == "open":
        w["last_closed_epoch"] = (NOW//60)*60
    c = context.analyze_context(w, market="forex", symbol="F1",
                                timeframe_seconds=60, observed_at=NOW)
    assert c["state"] == "INDETERMINATE"
    assert c["direction"] == "NEUTRAL"
    assert c["confidence"] == 0
    assert c["reason_codes"] == [code]
    assert c["research_usable"] is False
    assert c["execution_allowed"] is False


def test_context_uses_i01_to_exclude_open_candle(monkeypatch):
    async def run():
        # Include synthetic candle that starts in the currently OPEN minute.
        raw = _bars()
        raw["candles"].append({
            "epoch": (NOW//60)*60, "open": 118,
            "close": 130, "high": 131, "low": 117,
        })
        upstream = AsyncMock(return_value=raw)
        monkeypatch.setattr(market, "shared_candle_request", upstream)
        data = await market_data.read_closed_window("R_100", 60, 97, now=NOW)
        assert data["quality"] == "COMPLETE"
        assert data["closed_count"] == 96
        c = context.analyze_context(
            data, market="derived", symbol="R_100",
            timeframe_seconds=60, observed_at=NOW,
        )
        assert c["last_closed_epoch"] == (NOW//60-1)*60
        assert c["evidence"]["closed_bars_used"] == context.MIN_BARS
        upstream.assert_awaited_once()
    asyncio.run(run())


def test_dynamic_hierarchy_varies_market_family_volatility_and_expiry():
    forex = _candidate()
    derived = _candidate("R_100", "derived", family="volatility")
    stressed = _candidate(vol=.020, regime="HIGH_VOLATILITY")
    normal_1m = tf.choose_hierarchy(forex, expiry_minutes=1)
    deriv_5m = tf.choose_hierarchy(derived, expiry_minutes=5)
    stress_1m = tf.choose_hierarchy(stressed, expiry_minutes=1)
    forex_15m = tf.choose_hierarchy(forex, expiry_minutes=15)
    assert (normal_1m["higher_seconds"], normal_1m["intermediate_seconds"],
            normal_1m["lower_seconds"]) == (900, 300, 60)
    assert (deriv_5m["higher_seconds"], deriv_5m["intermediate_seconds"],
            deriv_5m["lower_seconds"]) == (900, 300, 60)
    assert (stress_1m["higher_seconds"], stress_1m["intermediate_seconds"],
            stress_1m["lower_seconds"]) == (3600, 300, 60)
    assert (forex_15m["higher_seconds"], forex_15m["intermediate_seconds"],
            forex_15m["lower_seconds"]) == (3600, 900, 300)
    assert all(not p["broker_contract_expiry_verified"]
               for p in (normal_1m, deriv_5m, stress_1m, forex_15m))
    with pytest.raises(ValueError):
        tf.choose_hierarchy(forex, expiry_minutes=2)
    with pytest.raises(ValueError):
        tf.choose_hierarchy(forex | {"execution_allowed": True}, expiry_minutes=5)
    with pytest.raises(ValueError):
        tf.choose_hierarchy(forex | {"realized_volatility": float("nan")},
                            expiry_minutes=5)
    with pytest.raises(ValueError):
        tf.choose_hierarchy(forex | {"candle_quality": "STALE"},
                            expiry_minutes=5)


def test_alignment_and_moderate_conflict_never_approve_trading():
    profile = tf.choose_hierarchy(_candidate(), expiry_minutes=5)
    contexts = {role: _ctx("UP") for role in tf.ROLE_NAMES}
    aligned = tf.align_timeframes(contexts, profile=profile)
    assert aligned["alignment"]["state"] == "ALIGNED_RESEARCH"
    assert aligned["alignment"]["direction"] == "UP"
    assert aligned["alignment"]["strategy_research_usable"] is True
    assert aligned["execution_allowed"] is False
    assert aligned["approved_for_trading"] is False
    contexts["lower"] = _ctx("DOWN", strength=.35, stability=.4, state="DOWNTREND")
    wait = tf.align_timeframes(contexts, profile=profile)
    assert wait["alignment"]["state"] == "WAIT_REEVALUATE"
    assert wait["alignment"]["requires_recheck"] is True
    assert wait["alignment"]["strategy_research_usable"] is False
    assert wait["overall_confidence"] == 0


def test_strong_conflicts_block_and_do_not_falsely_confirm_reversal():
    profile = tf.choose_hierarchy(_candidate(), expiry_minutes=5)
    contexts = {"higher": _ctx("UP", strength=.9), "intermediate": _ctx("DOWN"),
                "lower": _ctx("DOWN")}
    result = tf.align_timeframes(contexts, profile=profile)
    assert result["alignment"]["state"] == "BLOCKED_STRONG_CONFLICT"
    assert result["alignment"]["direction"] == "NEUTRAL"
    contexts["higher"] = _ctx("UP", strength=.4, stability=.5)
    contexts["lower"] = _ctx("DOWN", confirm=False)
    wait = tf.align_timeframes(contexts, profile=profile)
    assert wait["alignment"]["state"] == "WAIT_REEVALUATE"
    assert wait["execution_allowed"] is False


def test_reversal_requires_weak_higher_and_two_closed_confirmations():
    profile = tf.choose_hierarchy(_candidate(), expiry_minutes=15)
    contexts = {
        "higher": _ctx("UP", strength=.3, stability=.5),
        "intermediate": _ctx("DOWN", strength=.9, stability=.9, confirm=True),
        "lower": _ctx("DOWN", strength=.85, stability=.9, confirm=True),
    }
    change = tf.align_timeframes(contexts, profile=profile)
    assert change["alignment"]["state"] == "REVERSAL_CONFIRMED_RESEARCH"
    assert change["alignment"]["direction"] == "DOWN"
    assert change["alignment"]["requires_recheck"] is True
    assert change["approved_for_trading"] is False
    contexts["intermediate"] = _ctx("DOWN", strength=.9, stability=.9, confirm=False)
    assert tf.align_timeframes(contexts, profile=profile)["alignment"]["state"] == "WAIT_REEVALUATE"


def test_strong_higher_lower_opposition_with_neutral_middle_is_blocked():
    profile = tf.choose_hierarchy(_candidate(), expiry_minutes=5)
    contexts = {
        "higher": _ctx("UP", strength=.90, stability=.90),
        "intermediate": _ctx("NEUTRAL", strength=.1, state="RANGING"),
        "lower": _ctx("DOWN", strength=.90, stability=.90),
    }
    answer = tf.align_timeframes(contexts, profile=profile)
    assert answer["alignment"]["state"] == "BLOCKED_STRONG_CONFLICT"
    assert answer["alignment"]["strategy_research_usable"] is False
    assert answer["approved_for_trading"] is False


def test_invalid_or_irregular_any_timeframe_is_indeterminate():
    profile = tf.choose_hierarchy(_candidate(), expiry_minutes=3)
    contexts = {k: _ctx("UP") for k in tf.ROLE_NAMES}
    contexts["higher"] = _ctx("NEUTRAL", state="IRREGULAR", usable=False)
    assert tf.align_timeframes(contexts, profile=profile)["alignment"]["state"] == "INDETERMINATE"
    contexts["higher"] = _ctx("NEUTRAL", state="INDETERMINATE", usable=False)
    assert tf.align_timeframes(contexts, profile=profile)["alignment"]["state"] == "INDETERMINATE"


def test_integrates_selected_i02_with_i01_three_closed_timeframes(monkeypatch):
    async def run():
        item = _candidate("R_100", "derived", family="volatility")
        i02 = {"version": "intelligence2_i02_v1", "markets": {
            "derived": {"selected": [item], "reserves": [], "assessments": []}
        }}
        called = []
        async def fetch(symbol, seconds, count, *, now, timeout):
            called.append((symbol, seconds, count, now))
            return _window(seconds)
        monkeypatch.setattr(market_data, "read_closed_window", fetch)
        out = await tf.enrich_i02_report(i02, expiry_minutes=5, now=NOW)
        assert out["version"] == "intelligence2_i03_v1"
        assert out["i02_source_version"] == "intelligence2_i02_v1"
        assert out["context_evaluated"] == 1
        assert out["shared_public_knowledge"] is True
        report = out["markets"]["derived"]["selected"][0]["context_timeframe"]
        assert report["profile"]["lower_seconds"] == 60
        assert set(report["contexts"]) == set(tf.ROLE_NAMES)
        assert report["state"] == "ALIGNED_RESEARCH"
        assert all(c["state"] == "UPTREND" for c in report["contexts"].values())
        assert len(report["provenance_sha256"]) == 64
        assert called == [
            ("R_100", 900, 96, NOW), ("R_100", 300, 96, NOW),
            ("R_100", 60, 96, NOW),
        ]
        assert i02["markets"]["derived"]["selected"][0].get("context_timeframe") is None
        assert out["execution_allowed"] is False
        assert out["approved_for_trading"] is False
    asyncio.run(run())


def test_broker_unavailable_on_one_tf_fails_closed_without_fallback(monkeypatch):
    async def run():
        item = _candidate()
        i02 = {"version": "intelligence2_i02_v1", "markets": {
            "forex": {"selected": [item], "reserves": []},
        }}
        async def fetch(symbol, seconds, count, *, now, timeout):
            if seconds == 900:
                raise market.MarketError("offline")
            return _window(seconds)
        monkeypatch.setattr(market_data, "read_closed_window", fetch)
        out = await tf.enrich_i02_report(i02, now=NOW)
        detail = out["markets"]["forex"]["selected"][0]["context_timeframe"]
        assert detail["state"] == "INDETERMINATE"
        assert detail["contexts"]["intermediate"]["state"] == "INDETERMINATE"
        assert detail["alignment"]["strategy_research_usable"] is False
        assert detail["execution_allowed"] is False
    asyncio.run(run())


def test_integration_fair_capacity_and_concurrency(monkeypatch):
    async def run():
        markets = {}
        for group in ("a", "b", "c", "d"):
            markets[group] = {"selected": [
                _candidate(f"{group.upper()}{i}", group) for i in range(3)
            ], "reserves": []}
        i02 = {"version": "intelligence2_i02_v1", "markets": markets}
        active = peak = 0
        async def fetch(symbol, seconds, count, *, now, timeout):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(.002)
                return _window(seconds)
            finally:
                active -= 1
        monkeypatch.setattr(market_data, "read_closed_window", fetch)
        output = await tf.enrich_i02_report(i02, now=NOW)
        assert 1 < peak <= tf.MAX_CONTEXT_CONCURRENCY
        assert output["context_evaluated"] == 8
        assert output["context_deferred_capacity"] == 4
        assert output["context_coverage_complete"] is False
        for name in ("a", "b", "c", "d"):
            rows = output["markets"][name]["selected"]
            assert sum(row["context_timeframe"]["state"] != "NOT_EVALUATED_CAPACITY"
                       for row in rows) == 2
            assert all(row["execution_allowed"] is False for row in rows)
        assert output["orders_sent"] == 0
    asyncio.run(run())


def test_same_public_context_for_demo_and_real_with_private_contract_overlay(
    monkeypatch, tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path}/i03.sqlite")
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setenv("NEXORA_INTELLIGENCE_READONLY_ENABLED", "true")
    monkeypatch.setenv("NEXORA_REAL_AUTOPILOT_ENABLED", "false")
    monkeypatch.setenv("NEXORA_REAL_RELEASE_APPROVED", "false")
    with factory() as session:
        demo = db.User(deriv_account_id="V_I03_DEMO", selected_mode="demo")
        real = db.User(deriv_account_id="CR_I03_REAL", selected_mode="real")
        session.add_all([demo,real])
        session.flush()
        demo_id, real_id = demo.id, real.id
        for uid, sid in ((demo_id,"i03-demo"),(real_id,"i03-real")):
            session.add(db.Session(session_hash=hashsecret(sid),
                user_id=uid, token_encrypted=encrypt("synthetic-only"),
                expires_at=db.utcnow()+timedelta(hours=1)))
        session.add(db.DemoOrder(
            user_id=demo_id, request_key="i03_demo_1",
            active_slot=demo_id, state="UNKNOWN", symbol="DEMO_OPEN",
            direction="CALL", duration_minutes=1, stake="1.00"))
        session.add(db.RealOrder(
            user_id=real_id, account_id="CR_I03_REAL",
            request_key="i03_real_1", active_slot="CR_I03_REAL",
            state="PENDING", symbol="REAL_OPEN", direction="PUT",
            duration_minutes=5, stake="2.00", currency="USD"))
        session.commit()

    mock_context = {
        "version": "intelligence2_i03_v1",
        "markets": {"forex":{"selected":[{
            "symbol":"FRX001", "context_timeframe":{
                "version": tf.VERSION, "state":"ALIGNED_RESEARCH",
                "execution_allowed":False, "approved_for_trading":False,
            },
        }], "reserves":[]}},
        "orders_sent": 0, "execution_allowed":False,
    }
    scanner = AsyncMock(return_value=mock_context)
    monkeypatch.setattr(main, "scan_intelligence_context", scanner)
    monkeypatch.setattr(main, "_asset_scanner_research_cache", {})
    monkeypatch.setattr(main, "_asset_scanner_cache", None)
    monkeypatch.setattr(main, "_asset_scanner_cache_time", 0.0)
    monkeypatch.setattr(main, "_asset_scanner_cache_version", 1)
    monkeypatch.setattr(main, "_asset_scanner_cache_expiry_minutes", None)
    monkeypatch.setattr(main, "_asset_scanner_cache_epoch_bucket", None)
    monkeypatch.setattr(main, "_asset_scanner_last_v2", None)
    buy = AsyncMock(side_effect=AssertionError("I03 forbidden to buy"))
    monkeypatch.setattr(real_broker, "buy_real_contract", buy)

    client = TestClient(main.app)
    try:
        assert client.get("/api/research/asset-scanner?version=3").status_code == 401
        client.cookies.set("nexora_sid", "i03-demo")
        first = client.get("/api/research/asset-scanner?version=3&expiry_minutes=5")
        assert first.status_code == 200
        assert first.json()["cached"] is False
        assert {x["symbol"] for x in first.json()["preserved_open_contracts"]} == {
            "DEMO_OPEN"
        }
        assert first.json()["markets"] == mock_context["markets"]
        assert client.get("/api/research/asset-scanner?version=3&expiry_minutes=5").json()["cached"] is True
        client.cookies.set("nexora_sid", "i03-real")
        second = client.get("/api/research/asset-scanner?version=3&expiry_minutes=5")
        assert second.status_code == 200
        assert second.json()["cached"] is True
        assert {x["symbol"] for x in second.json()["preserved_open_contracts"]} == {
            "REAL_OPEN"
        }
        assert second.json()["markets"] == first.json()["markets"]
        scanner.assert_awaited_once()
        # The public hierarchy parameter is part of the EXISTING report cache
        # key, never mixed across expiries/accounts.
        third = client.get("/api/research/asset-scanner?version=3&expiry_minutes=15")
        assert third.status_code == 200
        assert third.json()["cached"] is False
        assert scanner.await_count == 2
        assert client.get("/api/research/asset-scanner?version=3&expiry_minutes=2").status_code == 422
        assert client.get("/api/research/asset-scanner?version=9").status_code == 422
        with factory() as session:
            assert session.scalar(select(db.DemoOrder.state).where(
                db.DemoOrder.user_id == demo_id)) == "UNKNOWN"
            assert session.scalar(select(db.RealOrder.state).where(
                db.RealOrder.user_id == real_id)) == "PENDING"
        buy.assert_not_awaited()
    finally:
        client.close()
        engine.dispose()


def test_i03_invalid_expiry_and_i02_payload_fail_closed():
    with pytest.raises(market.MarketError):
        asyncio.run(tf.enrich_i02_report({"markets": {}}, expiry_minutes=2, now=NOW))
    with pytest.raises(market.MarketError):
        asyncio.run(tf.enrich_i02_report({"markets": None}, expiry_minutes=5, now=NOW))

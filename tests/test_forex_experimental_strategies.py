"""Offline checks for the experimental Forex H1/M15 strategies and run state.

Tests use synthetic candles and mocked persistence; they never contact Deriv,
submit proposals, or place orders.
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import automation_signals, main, real_runtime
from app.expiry_policy import proposal_durations
from app.strategy_registry import FOREX_MTF_STRATEGIES, strategy_allowed


def _signal_close():
    return int(datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc).timestamp())


def _h1(direction, signal_close):
    sign = 1 if direction == "CALL" else -1
    last_open = signal_close - 3600
    first_open = last_open - 53 * 3600
    rows = []
    for index in range(54):
        close = 1.1 + sign * index * 0.001
        opening = close - sign * 0.0003
        rows.append({
            "epoch": first_open + index * 3600,
            "open": opening,
            "high": max(opening, close) + 0.0002,
            "low": min(opening, close) - 0.0002,
            "close": close,
        })
    return rows


def _m15(direction, signal_close, strategy):
    sign = 1 if direction == "CALL" else -1
    count = 55 if strategy == "fx_pullback_m15" else 21
    last_open = signal_close - 900
    first_open = last_open - (count - 1) * 900
    rows = []
    for index in range(count):
        close = (1.1 if direction == "CALL" else 1.2) + sign * index * 0.000005
        opening = close - sign * 0.000015
        rows.append({
            "epoch": first_open + index * 900,
            "open": opening,
            "high": max(opening, close) + 0.00002,
            "low": min(opening, close) - 0.00002,
            "close": close,
        })

    if strategy == "fx_pullback_m15":
        closes = [row["close"] for row in rows]
        ema = automation_signals._ema(closes, 50)
        touch_index = len(rows) - 3
        if direction == "CALL":
            rows[touch_index]["low"] = ema[touch_index]
        else:
            rows[touch_index]["high"] = ema[touch_index]

    previous = rows[-2]
    if direction == "CALL":
        close = previous["high"] + 0.000005
        opening = close - 0.000015
    else:
        close = previous["low"] - 0.000005
        opening = close + 0.000015
    rows[-1].update({
        "open": opening,
        "high": max(opening, close) + 0.00002,
        "low": min(opening, close) - 0.00002,
        "close": close,
    })
    return rows


@pytest.mark.parametrize(
    ("strategy", "direction"),
    [
        ("fx_pullback_m15", "CALL"),
        ("fx_pullback_m15", "PUT"),
        ("fx_breakout_m15", "CALL"),
        ("fx_breakout_m15", "PUT"),
    ],
)
def test_forex_signal_rules_confirm_both_directions_on_closed_bars(strategy, direction):
    close = _signal_close()
    result = automation_signals.forex_multitimeframe_signal(
        strategy,
        _h1(direction, close),
        _m15(direction, close, strategy),
        now=close + 5,
    )

    assert result is not None
    assert result["direction"] == direction
    assert result["minutes"] == 15
    assert result["expiry_options"] == [15]
    assert result["bar_seconds"] == 900
    assert result["deadline_epoch"] == close + 25
    assert result["last_bar_epoch"] == close - 900
    assert proposal_durations("", result["expiry_options"], automatic=False) == (15,)


def test_forex_signal_rejects_early_stale_and_discontinuous_data():
    close = _signal_close()
    h1 = _h1("CALL", close)
    m15 = _m15("CALL", close, "fx_breakout_m15")
    assert automation_signals.forex_multitimeframe_signal(
        "fx_breakout_m15", h1, m15, now=close - 1) is None
    assert automation_signals.forex_multitimeframe_signal(
        "fx_breakout_m15", h1, m15, now=close + 21) is None

    broken_m15 = [dict(row) for row in m15]
    broken_m15[-2]["epoch"] += 900
    assert automation_signals.forex_multitimeframe_signal(
        "fx_breakout_m15", h1, broken_m15, now=close + 5) is None

    broken_h1 = [dict(row) for row in h1]
    broken_h1[-2]["epoch"] += 3600
    assert automation_signals.forex_multitimeframe_signal(
        "fx_breakout_m15", broken_h1, m15, now=close + 5) is None


@pytest.mark.parametrize("account_mode", ["demo", "real"])
def test_forex_scanner_requests_h1_m15_and_discards_forming_candles(monkeypatch, account_mode):
    close = _signal_close()
    h1 = _h1("CALL", close)
    m15 = _m15("CALL", close, "fx_breakout_m15")
    # A broker response may include a still-forming bar after the completed ones.
    h1_payload = {"candles": h1 + [{
        "epoch": close, "open": 1.2, "high": 1.3, "low": 1.1, "close": 1.25,
    }]}
    m15_payload = {"candles": m15 + [{
        "epoch": close, "open": 1.2, "high": 1.3, "low": 1.1, "close": 1.25,
    }]}
    requested = []
    monkeypatch.setattr(automation_signals.time, "time", lambda: close + 5)

    async def supported(symbol, *, minutes):
        assert symbol == "frxEURUSD"
        assert minutes == 15
        return True

    async def candle_request(symbol, seconds, count, *, timeout):
        requested.append((symbol, seconds, count))
        return h1_payload if seconds == 3600 else m15_payload

    monkeypatch.setattr(automation_signals, "supported", supported)
    monkeypatch.setattr(automation_signals, "shared_candle_request", candle_request)
    automation_signals._scan_cache.clear()
    profile = SimpleNamespace(
        market="forex", account_mode=account_mode, strategy="fx_breakout_m15",
        symbols_json='["frxEURUSD"]', direction="AUTO",
    )
    try:
        result = asyncio.run(automation_signals.confirmed_automation_signal(profile))
    finally:
        automation_signals._scan_cache.clear()

    assert result is not None
    assert result["direction"] == "CALL"
    assert sorted(seconds for _, seconds, _ in requested) == [900, 3600]
    assert result["last_bar_epoch"] == close - 900


def test_new_strategies_are_forex_only_in_demo_and_real_and_existing_ids_remain_eligible():
    assert FOREX_MTF_STRATEGIES == {"fx_pullback_m15", "fx_breakout_m15"}
    for strategy in FOREX_MTF_STRATEGIES:
        assert strategy_allowed(strategy, "forex", "demo")
        assert strategy_allowed(strategy, "forex", "real")
        assert not strategy_allowed(strategy, "volatility", "demo")
        assert not strategy_allowed(strategy, "stocks_indices", "real")
        assert not strategy_allowed(strategy, "commodities", "real")
    for strategy in ("pulso_v2", "trend", "rsi"):
        for mode in ("demo", "real"):
            for market in ("forex", "volatility", "stocks_indices", "commodities"):
                assert strategy_allowed(strategy, market, mode)
        assert not strategy_allowed(strategy, "forex", "paper")


def test_candidate_entry_deadline_uses_candidate_timeframe_and_fails_closed():
    assert main.candidate_entry_deadline({
        "last_bar_epoch": 1800000000, "bar_seconds": 900,
    }) == 1800000912
    assert main.candidate_entry_deadline({
        "last_bar_epoch": 1800000000, "bar_seconds": 60,
        "deadline_epoch": 1800000025,
    }) == 1800000025
    assert main.candidate_entry_deadline({"last_bar_epoch": 1, "bar_seconds": 7200}) == 0
    assert main.candidate_entry_deadline(None) == 0


def test_real_candidate_window_honors_explicit_m15_deadline_and_fails_closed(monkeypatch):
    candidate = {
        "symbol": "frxEURUSD", "direction": "CALL",
        "last_bar_epoch": 1800000000, "bar_seconds": 900,
        "deadline_epoch": 1800000025,
    }
    monkeypatch.setattr(real_runtime.time, "time", lambda: 1800000010)
    assert real_runtime._candidate_window(candidate) == 1800000025
    assert real_runtime._candidate_window({
        "symbol": "frxEURUSD", "direction": "CALL",
        "last_bar_epoch": 1800000000, "bar_seconds": 900,
    }) == 1800000912
    assert real_runtime._candidate_window({
        "symbol": "frxEURUSD", "direction": "CALL",
        "last_bar_epoch": 1800000000, "bar_seconds": 7200,
    }) is None
    monkeypatch.setattr(real_runtime.time, "time", lambda: 1800000026)
    assert real_runtime._candidate_window(candidate) is None


@pytest.mark.parametrize("account_mode", ["demo", "real"])
def test_forex_profile_api_rejects_non_forex_strategy_without_network(monkeypatch, account_mode):
    monkeypatch.setattr(main, "active_session", lambda request, sid: (SimpleNamespace(id=7), "offline"))
    payload = main.AutomationProfileRequest(
        panel_mode="strategies", operation_mode="strategies", account_mode=account_mode,
        market="volatility",
        symbols=["frxEURUSD"],
        strategy="fx_pullback_m15",
    )
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.save_automation_profile(None, payload, "offline"))
    assert caught.value.status_code == 422
    assert "apenas em Forex" in caught.value.detail


def test_new_forex_profile_persists_in_demo_without_generic_indicators(monkeypatch):
    from app import db

    class MemorySession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, model, user_id):
            return None

        def add(self, item):
            pass

        def commit(self):
            pass

    monkeypatch.setattr(main, "active_session", lambda request, sid: (SimpleNamespace(id=812), "offline"))
    monkeypatch.setattr(main.db, "SessionLocal", MemorySession)

    async def instruments(group):
        assert group == "forex"
        return [{"symbol": "frxEURUSD"}]

    monkeypatch.setattr(main, "featured_instruments_by_group", instruments)
    payload = main.AutomationProfileRequest(
        panel_mode="strategies", operation_mode="strategies", account_mode="demo",
        market="forex",
        symbols=["frxEURUSD"],
        strategy="fx_pullback_m15",
        indicators=["EMA"],
        direction="AUTO",
        min_payout_pct=88, stake="0.50", stop_loss="2.00", stop_win="0.00",
        martingale=False, gale_count=0, cycles=0, multiplier="2.00", cycle_multiplier="2.00",
        expiry_minutes=15,
    )
    saved = asyncio.run(main.save_automation_profile(None, payload, "offline"))
    assert saved["saved"] is True
    assert saved["strategy"] == "fx_pullback_m15"
    assert saved["account_mode"] == "demo"
    assert saved["market"] == "forex"
    assert saved["indicators"] == []


def test_new_forex_profile_persists_in_real_with_broker_apis_mocked(monkeypatch):
    from app import auth_helpers, db

    class MemorySession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, model, user_id):
            return None

        def add(self, item):
            pass

        def commit(self):
            pass

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"accounts": []}

    class MockAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(main, "active_session", lambda request, sid: (SimpleNamespace(id=813), "offline"))
    monkeypatch.setattr(db, "SessionLocal", MemorySession)
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: MockAsyncClient())
    monkeypatch.setattr(auth_helpers, "extract_accounts", lambda payload: [])
    monkeypatch.setattr(auth_helpers, "real_accounts", lambda rows: [{"account_id": "R123"}])

    async def instruments(group):
        assert group == "forex"
        return [{"symbol": "frxEURUSD"}]

    monkeypatch.setattr(main, "featured_instruments_by_group", instruments)
    payload = main.AutomationProfileRequest(
        panel_mode="strategies", operation_mode="strategies", account_mode="real", market="forex", symbols=["frxEURUSD"],
        strategy="fx_pullback_m15", real_account_id="R123",
        indicators=["EMA"], direction="AUTO", min_payout_pct=88,
        expiry_minutes=15,
    )
    saved = asyncio.run(main.save_automation_profile(None, payload, "offline"))
    assert saved["saved"] is True
    assert saved["strategy"] == "fx_pullback_m15"
    assert saved["account_mode"] == "real"
    assert saved["market"] == "forex"
    assert saved["expiry_minutes"] == 15
    assert saved["indicators"] == []
    assert saved["execution"] == "DERIV_REAL_CONFIGURED"


def test_real_activation_accepts_forex_experimental_without_sending_orders(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from test_release_modes import client_with_user
    from app import auth_helpers, db

    client, engine, uid = client_with_user(tmp_path, monkeypatch)
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"accounts": []}

    class MockAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return Response()

    with db.SessionLocal() as session:
        user = session.get(db.User, uid)
        user.stake = "0.50"
        user.stop_loss = "2.00"
        user.martingale = False
        profile = db.AutomationProfile(
            user_id=uid, market="forex", symbols_json='["frxEURUSD"]',
            strategy="fx_breakout_m15", account_mode="real",
            expiry_minutes=15, news_filter=False, min_payout="88",
            max_daily_orders=0, indicators_json="[]", direction="AUTO",
            real_account_id="R123", preset_mode="custom",
        )
        session.add(profile)
        session.commit()

    monkeypatch.setattr(real_runtime, "real_available", lambda: True)
    monkeypatch.setattr(real_runtime, "real_tick", AsyncMock())
    monkeypatch.setattr(main, "access_status", lambda sess, user_id: {"allowed": True})
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: MockAsyncClient())
    monkeypatch.setattr(auth_helpers, "extract_accounts", lambda payload: [])
    monkeypatch.setattr(auth_helpers, "real_accounts", lambda rows: [{"account_id": "R123"}])
    monkeypatch.setattr(main, "real_account_balance", AsyncMock(return_value={
        "currency": "USD", "balance": "10.00",
    }))
    buy = AsyncMock(side_effect=AssertionError("REAL buy must never run in this test"))
    monkeypatch.setattr(real_runtime, "buy_real_contract", buy)
    real_runtime.running.discard(uid)
    try:
        response = client.post(
            "/api/autopilot/start", json={"confirm":"START_REAL_AUTOPILOT",'panel_mode':'strategies'}
        )
        assert response.status_code == 200, response.text
        assert response.json()["running"] is True
        assert response.json()["execution"] == "DERIV_REAL"
        buy.assert_not_awaited()
        with db.SessionLocal() as session:
            assert session.query(db.RealOrder).count() == 0
    finally:
        real_runtime.running.discard(uid)
        real_runtime.last_scan.pop(uid, None)
        engine.dispose()


def test_forex_experimental_choices_are_real_visible_and_clearly_disclosed():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "app" / "index.html").read_text(encoding="utf-8")
    assert 'data-forex-experimental-strategy' in html
    assert "const eligible=automationRadio('autoMarket')==='forex';" in html
    assert "Treinamento/DEMO e REAL" in html
    assert "Em REAL, contratos podem gerar perdas financeiras" in html
    assert "isForexExperimentalStrategy" in html
    assert "fixa M15 (15 minutos)" in html
    assert "A duração selecionada será verificada pela Deriv e pelo payout mínimo" in html
    assert "fixa M15 (15 minutos)" in html


def test_status_syncs_persisted_demo_run_and_blocks_duplicate_start(tmp_path, monkeypatch):
    from test_demopilot import cleanup, prepare
    from app import db
    from unittest.mock import AsyncMock

    client, factory, engine, uid = prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "demo_buy", AsyncMock(side_effect=AssertionError("must not buy")))
    try:
        started = client.post(
            "/api/autopilot/start", json={"confirm":"START_DEMO_AUTOPILOT",'panel_mode':'strategies'}
        )
        assert started.status_code == 200

        # Simulate another request worker/browser process with no local set.
        main.autopilot_enabled.discard(uid)
        state = client.get("/api/status?panel_mode=strategies").json()
        assert state["running"] is True
        assert state["demo_run_intent"] is True
        assert state["running_local"] is False

        duplicate = client.post(
            "/api/autopilot/start", json={"confirm":"START_DEMO_AUTOPILOT",'panel_mode':'strategies'}
        )
        assert duplicate.status_code == 409
        assert "armada na nuvem" in duplicate.json()["detail"]
        with factory() as session:
            assert session.get(db.RobotRun, uid) is not None
            assert session.get(db.AutoPilotResumeConsent, uid) is not None

        stopped = client.post("/api/autopilot/stop",json={"panel_mode":"strategies"})
        assert stopped.status_code == 200
        assert client.get("/api/status?panel_mode=strategies").json()["running"] is False
        with factory() as session:
            assert session.get(db.RobotRun, uid) is None
            assert session.get(db.AutoPilotResumeConsent, uid) is None
        main.demo_buy.assert_not_awaited()
    finally:
        cleanup(engine)

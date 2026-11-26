"""Strategy-owned analysis: legacy generic indicators never affect signals.

All tests are offline; they make no broker requests or trade orders.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import automation_signals, pulso_live


def make_profile(strategy, indicators_json='["ATR","EMA","RSI"]', direction="AUTO"):
    return SimpleNamespace(
        symbols_json='["R_100"]',
        indicators_json=indicators_json,
        direction=direction,
        strategy=strategy,
        expiry_minutes=5,
    )


def rows_from_closes(closes, last_open=None):
    rows = []
    for index, close in enumerate(closes):
        opening = close
        if index == len(closes) - 1 and last_open is not None:
            opening = last_open
        rows.append(
            {
                "open": opening,
                "high": max(opening, close) + 0.5,
                "low": min(opening, close) - 0.5,
                "close": close,
            }
        )
    return rows


def test_profile_response_hides_legacy_indicator_choices():
    from app.main import automation_profile_result

    profile = SimpleNamespace(
        account_mode="demo",
        market="volatility",
        symbols_json='["R_100"]',
        strategy="pulso_v2",
        expiry_minutes=0,
        news_filter=False,
        news_minutes_before=30,
        news_minutes_after=30,
        indicators_json='["ATR","EMA"]',
        direction="PUT",
        operation_mode=None,
        real_account_id=None,
        min_payout="88",
        preset_mode="custom",
    )

    result = automation_profile_result(profile)
    assert result["indicators"] == []
    assert result["direction"] == "AUTO"


def test_saving_legacy_indicator_selections_clears_persisted_values(monkeypatch):
    from app import main

    profile = SimpleNamespace(
        account_mode="demo",
        market="volatility",
        symbols_json='["R_100"]',
        strategy="pulso_v2",
        expiry_minutes=5,
        active_panel_mode=None,
        news_filter=False,
        news_minutes_before=30,
        news_minutes_after=30,
        max_daily_orders=0,
        indicators_json='["ATR","EMA"]',
        direction="AUTO",
        real_account_id=None,
        min_payout="88",
        preset_mode="custom",
    )

    class MemorySession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, model, user_id):
            return profile

        def commit(self):
            pass

    session = MemorySession()
    monkeypatch.setattr(
        main,
        "active_session",
        lambda request, sid: (SimpleNamespace(id=987654321), "offline-token"),
    )

    async def instruments(group):
        assert group == "derived"
        return [{"symbol": "R_100"}]

    monkeypatch.setattr(main, "featured_instruments_by_group", instruments)
    monkeypatch.setattr(main.db, "ensure_panel_profiles", lambda session, uid: (profile, profile))
    monkeypatch.setattr(main.db, "SessionLocal", lambda: session)
    payload = main.AutomationProfileRequest(
        panel_mode="strategies",
        account_mode="demo",
        market="volatility",
        symbols=["R_100"],
        strategy="pulso_v2",
        indicators=["ATR", "EMA"],
    )

    saved = asyncio.run(main.save_automation_profile(None, payload, "offline"))

    assert saved["saved"] is True
    assert saved["indicators"] == []
    assert profile.indicators_json == "[]"


def test_legacy_indicators_are_not_forwarded_to_pulso(monkeypatch):
    received = {}

    async def fake_pulso(symbols, *, direction, minutes, scan_event):
        received.update(
            symbols=symbols,
            direction=direction,
            minutes=minutes,
            scan_event=scan_event,
        )
        return {"strategy": "pulso_v2", "direction": "CALL"}

    monkeypatch.setattr(automation_signals, "confirmed_pulso_signal", fake_pulso)
    profile = make_profile("pulso_v2", '["ATR","BOLLINGER","RSI"]')

    result = asyncio.run(automation_signals.confirmed_automation_signal(profile))

    assert result == {"strategy": "pulso_v2", "direction": "CALL"}
    assert received == {
        "symbols": ["R_100"],
        "direction": "AUTO",
        "minutes": 5,
        "scan_event": None,
    }


@pytest.mark.parametrize("strategy", ["trend", "rsi"])
def test_legacy_indicators_do_not_filter_strategy_owned_signal(monkeypatch, strategy):
    now = 126.0
    rows = [
        {
            "epoch": -4980 + 60 * index,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
        }
        for index in range(85)
    ]

    monkeypatch.setattr(automation_signals.time, "time", lambda: now)

    async def candles(*args, **kwargs):
        return []

    monkeypatch.setattr(automation_signals, "shared_candle_request", candles)
    monkeypatch.setattr(
        automation_signals,
        "strict_closed_bars",
        lambda raw, seconds, now: rows,
    )
    monkeypatch.setattr(
        automation_signals,
        "_direction_for",
        lambda candle_rows, selected_strategy: "CALL",
    )
    automation_signals._scan_cache.clear()
    profile = make_profile(strategy, '["ATR","BOLLINGER","CANDLE_PATTERNS"]', direction="PUT")

    try:
        result = asyncio.run(automation_signals.confirmed_automation_signal(profile))
    finally:
        automation_signals._scan_cache.clear()

    assert result is not None
    assert result["direction"] == "CALL"
    assert result["reason"] == strategy + " · fechamento M1"


@pytest.mark.parametrize(
    ("strategy", "direction"),
    [("trend", "CALL"), ("trend", "PUT"), ("rsi", "CALL"), ("rsi", "PUT")],
)
def test_strategy_owned_rules_still_produce_their_real_signals(strategy, direction):
    if strategy == "trend":
        base = [
            100 + 0.2 * index if direction == "CALL" else 120 - 0.2 * index
            for index in range(60)
        ]
        fast = automation_signals._ema(base, 20)[-1]
        previous = fast - 0.2 if direction == "CALL" else fast + 0.2
        closes = base + [previous]
        fast_previous = automation_signals._ema(closes, 20)[-1]
        current = fast_previous + 0.2 if direction == "CALL" else fast_previous - 0.2
        closes.append(current)
        opening = current - 0.01 if direction == "CALL" else current + 0.01
    elif direction == "CALL":
        closes = [100 - index for index in range(15)] + [86, 96]
        opening = 95
    else:
        closes = [100 + index for index in range(15)] + [114, 104]
        opening = 105

    rows = rows_from_closes(closes, last_open=opening)

    assert automation_signals._direction_for(rows, strategy) == direction


def test_pulso_keeps_its_internal_stable_volatility_rule(monkeypatch):
    bars = []
    price = 100.0
    for index in range(135):
        opening = price
        if index >= 123:
            close = opening + 0.6
            high = close + 0.2
            low = opening - 0.2
        else:
            close = opening
            high = opening + 0.5
            low = opening - 0.5
        bars.append(
            {
                "epoch": -7980 + 60 * index,
                "open": opening,
                "high": high,
                "low": low,
                "close": close,
            }
        )
        price = close
    monkeypatch.setattr(pulso_live.time, "time", lambda: 126.0)

    async def candles(*args, **kwargs):
        return []

    monkeypatch.setattr(pulso_live, "shared_candle_request", candles)
    monkeypatch.setattr(pulso_live, "strict_closed_bars", lambda raw, seconds, now: bars)

    result = asyncio.run(
        pulso_live._candidate_for_symbol(
            "R_100",
            120,
            deadline=128.0,
            direction="AUTO",
            minutes=5,
        )
    )

    assert result is not None
    assert result["direction"] == "CALL"
    assert result["reason"].startswith("PULSO V2: pressão eficiente + volatilidade estável")

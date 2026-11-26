import asyncio
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db, main, manual_signals, real_runtime, signal_cataloger as cataloger
from app.market import MarketError
from app.security import encrypt, hashsecret

UTC = timezone.utc


def candles_for_days(start: date, directions_by_day: dict[date, str | None],
                     *, slots: tuple[str, ...] = ("12:00",), granularity: int = 900):
    rows = []
    for day, direction in directions_by_day.items():
        for slot in slots:
            local_time = time.fromisoformat(slot)
            epoch = int(datetime.combine(day, local_time, tzinfo=UTC).timestamp())
            if direction is None:
                open_price, close_price = 1.0, 1.0
            elif direction == "CALL":
                open_price, close_price = 1.0, 2.0
            else:
                open_price, close_price = 2.0, 1.0
            rows.append({"epoch": epoch, "open": open_price, "close": close_price})
    return rows


def test_walk_forward_excludes_doji_from_directional_denominators_and_exports_candidate():
    start = date(2025, 1, 1)
    directions = {}
    for index in range(32):
        day = start + timedelta(days=index)
        if index < 24:
            # Four neutral candles are not directional observations. The 20
            # directional training samples contain 15 CALL and 5 PUT candles.
            directions[day] = None if index < 4 else ("CALL" if index < 19 else "PUT")
        else:
            directions[day] = "CALL"
    candles = candles_for_days(start, directions)

    result = cataloger.catalogue_time_slots(
        candles=candles,
        granularity_seconds=900,
        target_day="tomorrow",
        timezone_name="UTC",
        now=datetime(2026, 11, 25, 12, tzinfo=UTC),
    )

    assert len(result["signals"]) == 1
    signal = result["signals"][0]
    assert signal["time"] == "12:00"
    assert signal["direction"] == "CALL"
    assert signal["train_samples"] == 20
    assert signal["train_hits"] == 15
    assert signal["train_rate"] == pytest.approx(0.75)
    assert signal["walk_forward_samples"] == 8
    assert signal["walk_forward_hits"] == 8
    assert signal["walk_forward_rate"] == pytest.approx(1.0)
    assert signal["historical_samples"] == 28
    assert "não" in result["note"].lower()


def test_validation_outcomes_cannot_change_selected_candidate_or_exported_direction():
    start = date(2025, 1, 1)
    days = [start + timedelta(days=index) for index in range(40)]
    neutral_slot = {day: None for day in days}
    candles = candles_for_days(start, neutral_slot, slots=("00:00",))

    def catalogue_with_validation(validation_direction):
        # The first 20 directional training samples have 14 CALL and 6 PUT
        # (exactly the 70% selection threshold); the next 10 are dojis.
        slot_directions = {
            day: ("CALL" if index < 14 else "PUT") if index < 20
            else None if index < 30 else validation_direction
            for index, day in enumerate(days)
        }
        slot_candles = candles_for_days(start, slot_directions, slots=("12:00",))
        return cataloger.catalogue_time_slots(
            candles=candles + slot_candles,
            granularity_seconds=900,
            target_day="tomorrow",
            timezone_name="UTC",
            now=datetime(2026, 11, 25, 12, tzinfo=UTC),
        )

    all_put_validation = catalogue_with_validation("PUT")
    all_call_validation = catalogue_with_validation("CALL")

    put_signal = all_put_validation["signals"][0]
    call_signal = all_call_validation["signals"][0]
    assert [(row["time"], row["direction"]) for row in all_put_validation["signals"]] == [
        ("12:00", "CALL")
    ]
    assert [(row["time"], row["direction"]) for row in all_call_validation["signals"]] == [
        ("12:00", "CALL")
    ]
    assert put_signal["train_samples"] == call_signal["train_samples"] == 20
    assert put_signal["train_hits"] == call_signal["train_hits"] == 14
    assert put_signal["walk_forward_rate"] != call_signal["walk_forward_rate"]


def test_insufficient_walk_forward_validation_produces_no_candidate():
    start = date(2025, 1, 1)
    directions = {start + timedelta(days=i): "CALL" for i in range(30)}
    result = cataloger.catalogue_time_slots(
        candles=candles_for_days(start, directions),
        granularity_seconds=900,
        target_day="tomorrow",
        timezone_name="UTC",
        now=datetime(2026, 11, 25, 12, tzinfo=UTC),
    )
    assert result["signals"] == []
    assert "insuficiente" in result["note"].lower()


def test_catalogue_caps_candidates_at_five_in_clock_order():
    start = date(2025, 1, 1)
    slots = ("00:00", "01:00", "02:00", "03:00", "04:00", "05:00", "06:00")
    directions = {start + timedelta(days=i): "CALL" for i in range(32)}
    result = cataloger.catalogue_time_slots(
        candles=candles_for_days(start, directions, slots=slots),
        granularity_seconds=900,
        target_day="tomorrow",
        timezone_name="UTC",
        now=datetime(2026, 11, 25, 12, tzinfo=UTC),
    )
    assert result["candidates_before_daily_cap"] == 7
    assert [signal["time"] for signal in result["signals"]] == list(slots[:5])
    assert result["max_signals"] == 5


def test_target_date_uses_selected_local_timezone_not_server_timezone():
    result = cataloger.catalogue_time_slots(
        candles=[],
        granularity_seconds=900,
        target_day="tomorrow",
        timezone_name="America/Sao_Paulo",
        now=datetime(2026, 1, 2, 2, 0, tzinfo=UTC),
    )
    # 02:00 UTC on Jan 2 is 23:00 on Jan 1 in Brasília.
    assert result["target_date"] == "2026-01-02"
    assert result["history_days"] == 0


def test_today_keeps_future_slots_without_excluding_a_whole_timeframe():
    start = date(2025, 1, 1)
    directions = {start + timedelta(days=i): "CALL" for i in range(32)}
    result = cataloger.catalogue_time_slots(
        candles=candles_for_days(start, directions, slots=("12:00", "12:15")),
        granularity_seconds=900,
        target_day="today",
        timezone_name="UTC",
        now=datetime(2026, 11, 25, 12, 5, tzinfo=UTC),
    )
    assert [signal["time"] for signal in result["signals"]] == ["12:15"]


def test_catalogue_rejects_unsupported_request_values():
    with pytest.raises(ValueError, match="M5 ou M15"):
        cataloger.validate_request(
            symbol="R_100", granularity_seconds=60, lookback_days=60,
            target_day="today", timezone_name="UTC",
        )
    with pytest.raises(ValueError, match="Fuso"):
        cataloger.validate_request(
            symbol="R_100", granularity_seconds=300, lookback_days=60,
            target_day="today", timezone_name="Not/AZone",
        )


def test_forex_weekend_returns_clear_no_signal_result():
    result = cataloger.catalogue_time_slots(
        candles=[], granularity_seconds=900, target_day="today",
        timezone_name="UTC", now=datetime(2026, 11, 28, 12, tzinfo=UTC),
        market="forex",
    )
    assert result["signals"] == []
    assert "fechado" in result["note"].lower()


def test_fetch_history_pages_deduplicates_and_excludes_open_and_out_of_window_candles(monkeypatch):
    now = (1_800_000_000 // 300) * 300
    start = now - 30 * 86400
    first = [
        {"epoch": now, "open": 1, "close": 2},  # still open at request time
        {"epoch": now - 300, "open": 1, "close": 2},
        {"epoch": now - 600, "open": 2, "close": 1},
        {"epoch": now - 900, "open": 1, "close": 2},
    ]
    second = [
        {"epoch": now - 600, "open": 2, "close": 1},  # overlap from prior page
        {"epoch": start, "open": 1, "close": 2},
        {"epoch": start - 300, "open": 1, "close": 2},  # before lookback boundary
    ]
    request = AsyncMock(side_effect=[{"candles": first}, {"candles": second}])
    monkeypatch.setattr(cataloger, "public_request", request)
    monkeypatch.setattr(cataloger, "PAGE_SIZE", 4)
    cataloger._history_cache.clear()

    result = asyncio.run(cataloger.fetch_historical_candles(
        symbol="R_100", granularity_seconds=300, lookback_days=30,
        now_epoch=now,
    ))

    assert [row["epoch"] for row in result] == [start, now - 900, now - 600, now - 300]
    assert request.await_count == 2
    assert request.await_args_list[1].args[0]["end"] == str(now - 901)


def test_fetch_history_fails_closed_if_pagination_does_not_advance(monkeypatch):
    now = (1_800_000_000 // 300) * 300
    repeated = {"candles": [
        {"epoch": now - 300, "open": 1, "close": 2},
        {"epoch": now - 600, "open": 1, "close": 2},
        {"epoch": now - 900, "open": 1, "close": 2},
    ]}
    request = AsyncMock(return_value=repeated)
    monkeypatch.setattr(cataloger, "public_request", request)
    monkeypatch.setattr(cataloger, "PAGE_SIZE", 3)
    cataloger._history_cache.clear()

    with pytest.raises(MarketError, match="não avançou"):
        asyncio.run(cataloger.fetch_historical_candles(
            symbol="R_100", granularity_seconds=300, lookback_days=30,
            now_epoch=now,
        ))


def test_fetch_history_does_not_cache_or_return_partial_data_at_page_cap(monkeypatch):
    now = (1_800_000_000 // 300) * 300
    page = {"candles": [
        {"epoch": now - 300, "open": 1, "close": 2},
        {"epoch": now - 600, "open": 1, "close": 2},
        {"epoch": now - 900, "open": 1, "close": 2},
    ]}
    second_page = {"candles": [
        {"epoch": now - 1200, "open": 1, "close": 2},
        {"epoch": now - 1500, "open": 1, "close": 2},
        {"epoch": now - 1800, "open": 1, "close": 2},
    ]}
    request = AsyncMock(side_effect=[page, second_page])
    monkeypatch.setattr(cataloger, "public_request", request)
    monkeypatch.setattr(cataloger, "PAGE_SIZE", 3)
    monkeypatch.setattr(cataloger, "MAX_PAGES", 2)
    cataloger._history_cache.clear()

    with pytest.raises(MarketError, match="limite de páginas"):
        asyncio.run(cataloger.fetch_historical_candles(
            symbol="R_100", granularity_seconds=300, lookback_days=30,
            now_epoch=now,
        ))
    assert not cataloger._history_cache


def add_user(factory):
    sid = "cataloger-session-cookie"
    with factory() as session:
        user = db.User(deriv_account_id="V9001", selected_mode="demo",
                       stake="2.00", stop_win="20", stop_loss="10")
        session.add(user)
        session.flush()
        session.add(db.Session(
            session_hash=hashsecret(sid), user_id=user.id,
            token_encrypted=encrypt("mock-token"),
            expires_at=db.utcnow() + timedelta(hours=2),
        ))
        session.commit()
        return sid


@pytest.fixture
def cataloger_api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/cataloger.sqlite")
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(main.db, "SessionLocal", factory)
    client = TestClient(main.app)
    yield client, factory
    engine.dispose()


def test_cataloger_assets_and_generate_require_login(cataloger_api):
    client, _ = cataloger_api
    assert client.get("/api/cataloger/assets?market=derived").status_code == 401
    assert client.post("/api/cataloger/generate", json={
        "market": "derived", "symbol": "R_100",
    }).status_code == 401


def test_cataloger_endpoint_checks_catalog_membership_and_is_read_only(cataloger_api, monkeypatch):
    client, factory = cataloger_api
    sid = add_user(factory)
    client.cookies.set("nexora_sid", sid)
    listed = [{"symbol": "R_100", "name": "Volatility 100", "group": "derived"}]
    async def items_for_market(market):
        return listed if market == "derived" else []

    monkeypatch.setattr(main, "instruments_by_group", AsyncMock(side_effect=items_for_market))
    history_start = date(2025, 1, 1)
    historical = {history_start + timedelta(days=i): "CALL" for i in range(32)}
    history = AsyncMock(return_value=candles_for_days(history_start, historical))
    monkeypatch.setattr(main.signal_cataloger, "fetch_historical_candles", history)
    demo_buy = AsyncMock(side_effect=AssertionError("Cataloger must never buy"))
    demo_adapter = AsyncMock(side_effect=AssertionError("Cataloger must never execute DEMO"))
    real_adapter = AsyncMock(side_effect=AssertionError("Cataloger must never execute REAL"))
    monkeypatch.setattr(main, "demo_buy", demo_buy)
    monkeypatch.setattr(main, "perform_demo_buy", demo_adapter)
    monkeypatch.setattr(real_runtime, "buy_real_contract", real_adapter)

    assets = client.get("/api/cataloger/assets?market=derived")
    assert assets.status_code == 200
    assert assets.json()["items"] == [{"symbol": "R_100", "name": "Volatility 100"}]
    assert assets.json()["timezones"]["America/Sao_Paulo"].startswith("Brasília")

    mismatch = client.post("/api/cataloger/generate", json={
        "market": "forex", "symbol": "R_100", "granularity_seconds": 900,
        "lookback_days": 60, "target_day": "tomorrow",
        "timezone_name": "America/Sao_Paulo",
    })
    assert mismatch.status_code == 422
    assert not history.called

    response = client.post("/api/cataloger/generate", json={
        "market": "derived", "symbol": "R_100", "granularity_seconds": 900,
        "lookback_days": 60, "target_day": "tomorrow",
        "timezone_name": "America/Sao_Paulo",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["read_only"] is True and body["execution_allowed"] is False
    assert body["signals"]
    formatted = [signal["formatted"] for signal in body["signals"]]
    assert all(line.startswith("M15 R_100 ") for line in formatted)
    preview = manual_signals.validate_signal_list(
        text="\n".join(formatted), market="derived", day="tomorrow",
        timezone_name="America/Sao_Paulo", catalogue=listed,
    )
    assert len(preview["valid"]) == len(formatted)
    assert not preview["issues"]
    assert history.await_count == 1
    assert not demo_buy.called and not demo_adapter.called and not real_adapter.called


def test_cataloger_endpoint_maps_public_history_failure_to_gateway_error(cataloger_api, monkeypatch):
    client, factory = cataloger_api
    client.cookies.set("nexora_sid", add_user(factory))
    monkeypatch.setattr(main, "instruments_by_group", AsyncMock(return_value=[
        {"symbol": "R_100", "name": "Volatility 100", "group": "derived"},
    ]))
    monkeypatch.setattr(main.signal_cataloger, "fetch_historical_candles",
                        AsyncMock(side_effect=MarketError("Deriv unavailable")))

    response = client.post("/api/cataloger/generate", json={
        "market": "derived", "symbol": "R_100", "granularity_seconds": 300,
        "lookback_days": 30, "target_day": "today",
        "timezone_name": "UTC",
    })
    assert response.status_code == 502
    assert response.json()["detail"] == "Deriv unavailable"

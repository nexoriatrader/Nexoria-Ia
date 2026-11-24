"""Integrated NEXORA public OHLC research checks; no live orders or feed in CI."""
import asyncio
import time
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db, main, market, real_broker
from app.intelligence2.public_candle_quality import inspect_public_candles
from app.security import encrypt, hashsecret


def _candles(*, frame=60, length=60, end_offset=0):
    bucket = int(time.time()) // frame
    last_epoch = (bucket - 1 - end_offset) * frame
    first_epoch = last_epoch - (length - 1) * frame
    data = []
    for i in range(length):
        price = round(100 + i / 10, 4)
        data.append({
            "epoch": first_epoch + frame * i,
            "open": price, "high": round(price + 0.25, 4),
            "low": round(price - 0.25, 4),
            "close": round(price + 0.1, 4),
        })
    return data


@pytest.fixture
def authenticated(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/nexora-public-ohlc.sqlite")
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    with factory() as session:
        user = db.User(deriv_account_id="OHLC_RESEARCH_DEMO", selected_mode="demo")
        session.add(user)
        session.flush()
        session.add(db.Session(
            session_hash=hashsecret("ohlc_quality_demo_cookie"), user_id=user.id,
            token_encrypted=encrypt("test-token-not-used"),
            expires_at=db.utcnow() + timedelta(hours=1),
        ))
        session.commit()
    client = TestClient(main.app)
    client.cookies.set("nexora_sid", "ohlc_quality_demo_cookie")
    try:
        yield client
    finally:
        client.close()
        engine.dispose()


def test_integrated_authenticated_endpoint_reads_public_candles_only(authenticated, monkeypatch):
    closed = _candles()
    still_open = dict(closed[-1], epoch=(int(time.time()) // 60) * 60)
    upstream = AsyncMock(return_value={"candles": closed + [still_open]})
    forbidden_demo = AsyncMock(side_effect=AssertionError("DEMO buy forbidden"))
    forbidden_real = AsyncMock(side_effect=AssertionError("REAL buy forbidden"))
    monkeypatch.setattr(market, "shared_candle_request", upstream)
    monkeypatch.setattr(main, "demo_buy", forbidden_demo)
    monkeypatch.setattr(real_broker, "buy_real_contract", forbidden_real)
    response = authenticated.get(
        "/api/intelligence20/market/quality?symbol=R_100&granularity_seconds=60&closed_candles=60")
    assert response.status_code == 200, response.text
    data = response.json()
    upstream.assert_awaited_once_with("R_100", 60, 61, timeout=8.0)
    assert data["quality"] == "COMPLETE"
    assert data["data_available"] is True
    assert data["received_closed_candles"] == 60
    assert data["origin_path"] == "DERIV_PUBLIC_CLIENT_OR_SHARED_PUBLIC_CACHE"
    assert data["source_independently_certified"] is False
    assert data["read_only"] is True
    assert data["execution_allowed"] is False
    assert data["trade_recommendation"] is None
    assert data["predicted_outcome"] is None
    assert data["orders_sent"] == 0
    assert len(data["snapshot_sha256"]) == 64
    assert data["descriptive_statistics"]["observed_candles"] == 60
    assert data["descriptive_statistics"]["last_closed_epoch"] == closed[-1]["epoch"]
    assert data["descriptive_statistics"]["minimum_close"] <= data["descriptive_statistics"]["maximum_close"]
    assert "candles" not in data  # No unbounded broker payload shown to clients.
    forbidden_demo.assert_not_awaited()
    forbidden_real.assert_not_awaited()


@pytest.mark.parametrize("case, expected", [
    ("lag", "STALE"),
    ("gap", "GAPPED"),
    ("short", "INSUFFICIENT"),
])
def test_unusable_market_data_never_receives_statistics(monkeypatch, case, expected):
    bars = _candles(end_offset=2 if case == "lag" else 0)
    if case == "gap":
        bars.pop(20)
    if case == "short":
        bars = bars[-15:]
    upstream = AsyncMock(return_value={"candles": bars})
    monkeypatch.setattr(market, "shared_candle_request", upstream)
    data = asyncio.run(inspect_public_candles(symbol="R_100", closed_candles=60))
    assert data["quality"] == expected
    assert data["data_available"] is False
    assert data["descriptive_statistics"] is None
    assert data["snapshot_sha256"] is None
    if case == "gap":
        assert data["missing_intervals"] == 1


def test_authenticated_upstream_failure_is_real_503_no_fabricated_result(
        authenticated, monkeypatch):
    monkeypatch.setattr(market, "shared_candle_request", AsyncMock(
        side_effect=market.MarketError("public Deriv unavailable")))
    response = authenticated.get("/api/intelligence20/market/quality")
    assert response.status_code == 503
    assert "indisponíveis" in response.text
    assert "descriptive_statistics" not in response.text


@pytest.mark.parametrize("query", [
    "symbol=../admin", "symbol=ABC%20DEF", "granularity_seconds=30",
    "closed_candles=999", "closed_candles=0", "symbol=X",
])
def test_invalid_public_data_requests_do_not_contact_deriv(
        authenticated, monkeypatch, query):
    upstream = AsyncMock(side_effect=AssertionError("Invalid query must not fetch market data"))
    monkeypatch.setattr(market, "shared_candle_request", upstream)
    response = authenticated.get("/api/intelligence20/market/quality?" + query)
    assert response.status_code == 422
    upstream.assert_not_awaited()


def test_market_data_research_requires_session_and_does_not_edit_settings(
        authenticated, monkeypatch):
    upstream = AsyncMock(return_value={"candles": _candles()})
    monkeypatch.setattr(market, "shared_candle_request", upstream)
    without_cookie = TestClient(main.app)
    assert without_cookie.get("/api/intelligence20/market/quality").status_code == 401
    upstream.assert_not_awaited()
    with db.SessionLocal() as session:
        assert session.query(db.RobotUsageMode).count() == 0
        assert session.query(db.RobotRun).count() == 0
        assert session.query(db.DemoOrder).count() == 0


def test_integrated_panel_has_real_data_control_not_a_prediction():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "app/index.html").read_text(
        encoding="utf-8")
    assert 'id="nxPublicCandleCheck"' in html
    assert "function nxCheckMarketData()" in html
    assert "/api/intelligence20/market/quality?symbol=" in html
    assert "A origem não tem certificação independente" in html
    assert "Dados indisponíveis" in html
    assert "trade_recommendation!==null" in html

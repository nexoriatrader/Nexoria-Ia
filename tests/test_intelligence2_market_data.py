"""Intelligence 2.0 Market Data Infrastructure integration.

Everything is synthetic: no network, authorization, database or broker orders.
"""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app import market
from app.intelligence2.market_data import read_closed_window

NOW = 10_024  # Last closed M1 candle begins at 9_960.


def _row(epoch, *, opening="100", high="102", low="99", closing="101"):
    return {
        "epoch": epoch, "open": opening, "high": high,
        "low": low, "close": closing,
    }


def _complete():
    return {"candles": [_row(9_840), _row(9_900), _row(9_960)]}


def _read(monkeypatch, payload, *, now=NOW, symbol="R_100", count=3, granularity=60):
    upstream = AsyncMock(return_value=payload)
    monkeypatch.setattr(market, "shared_candle_request", upstream)
    output = asyncio.run(read_closed_window(symbol, granularity, count, now=now))
    return output, upstream


def test_reuses_existing_public_reader_and_closed_ohlc(monkeypatch):
    response = _complete()
    original = deepcopy(response)
    result, upstream = _read(monkeypatch, response)
    upstream.assert_awaited_once_with("R_100", 60, 3, timeout=8.0)
    assert response == original  # Immutable upstream input.
    assert result["source"] == "existing_public_candle_feed"
    assert result["quality"] == "COMPLETE"
    assert result["usable_for_research"] is True
    assert result["closed_count"] == 3
    assert result["missing_intervals"] == 0
    assert result["last_closed_epoch"] == 9_960
    assert result["execution_allowed"] is False
    assert result["read_only"] is True
    assert result["candles"][-1]["close"] == 101.0


def test_excludes_still_open_candle(monkeypatch):
    payload = _complete()
    payload["candles"].append(_row(10_020))
    result, _ = _read(monkeypatch, payload, count=4)
    assert result["quality"] == "COMPLETE"
    assert result["closed_count"] == 3
    assert all(item["epoch"] < 10_020 for item in result["candles"])


def test_gap_remains_a_gap_not_a_synthetic_candle(monkeypatch):
    result, _ = _read(monkeypatch, {"candles": [_row(9_840), _row(9_960)]})
    assert result["quality"] == "GAPPED"
    assert result["missing_intervals"] == 1
    assert result["usable_for_research"] is False
    assert [bar["epoch"] for bar in result["candles"]] == [9_840, 9_960]


def test_stale_window_never_claims_research_readiness(monkeypatch):
    result, _ = _read(monkeypatch, _complete(), now=10_084)
    assert result["quality"] == "STALE"
    assert result["expected_last_closed_epoch"] == 10_020
    assert result["usable_for_research"] is False


def test_insufficient_window_never_claims_research_readiness(monkeypatch):
    result, _ = _read(monkeypatch, {"candles": [_row(9_960)]})
    assert result["quality"] == "INSUFFICIENT"
    assert result["usable_for_research"] is False


@pytest.mark.parametrize("bad", [
    {"candles": [_row(9_900), _row(9_900), _row(9_960)]},
    {"candles": [_row(9_900, high="100", closing="101"), _row(9_960)]},
    {"candles": [_row(9_900, closing="NaN"), _row(9_960)]},
    {"candles": [_row(9_910), _row(9_960)]},
    {"candles": "not-a-list"},
    None,
])
def test_invalid_market_data_fails_closed(monkeypatch, bad):
    monkeypatch.setattr(market, "shared_candle_request", AsyncMock(return_value=bad))
    with pytest.raises(market.MarketError):
        asyncio.run(read_closed_window("R_100", 60, 3, now=NOW))


@pytest.mark.parametrize("symbol,tf,count,when,timeout", [
    ("../other", 60, 3, NOW, 8),
    ("R_100", 15, 3, NOW, 8),
    ("R_100", True, 3, NOW, 8),
    ("R_100", 60, 0, NOW, 8),
    ("R_100", 60, True, NOW, 8),
    ("R_100", 60, 3, float("nan"), 8),
    ("R_100", 60, 3, NOW, 0),
    ("R_100", 60, 3, NOW, float("inf")),
])
def test_bad_parameters_rejected_before_upstream(monkeypatch, symbol, tf, count, when, timeout):
    upstream = AsyncMock(side_effect=AssertionError("No upstream request allowed"))
    monkeypatch.setattr(market, "shared_candle_request", upstream)
    with pytest.raises(market.MarketError):
        asyncio.run(read_closed_window(symbol, tf, count, now=when, timeout=timeout))
    upstream.assert_not_awaited()


def test_upstream_failure_is_not_relabelled_as_valid_data(monkeypatch):
    upstream = AsyncMock(side_effect=market.MarketError("offline"))
    monkeypatch.setattr(market, "shared_candle_request", upstream)
    with pytest.raises(market.MarketError, match="offline"):
        asyncio.run(read_closed_window("R_100", 60, 3, now=NOW))
    upstream.assert_awaited_once()


def test_no_broker_execution_or_risk_authorization(monkeypatch):
    """The integration has no trading side effects, even when candle quality is OK."""
    from app import real_broker
    forbidden = AsyncMock(side_effect=AssertionError("Financial broker forbidden"))
    monkeypatch.setattr(real_broker, "buy_real_contract", forbidden)
    result, _ = _read(monkeypatch, _complete())
    assert result["usable_for_research"] is True
    assert result["execution_allowed"] is False
    forbidden.assert_not_awaited()

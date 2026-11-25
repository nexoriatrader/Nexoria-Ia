from datetime import datetime, timezone

import pytest

from app.manual_signals import ManualSignalInputError, validate_signal_list


CATALOGUE = [
    {"symbol": "frxEURUSD", "name": "EUR/USD", "group": "forex"},
    {"symbol": "frxAUDCAD", "name": "AUD/CAD", "group": "forex"},
    {"symbol": "R_45", "name": "Volatility 45", "group": "derived"},
]


def test_mixed_market_excludes_only_incompatible_line_and_keeps_valid_lines():
    result = validate_signal_list(
        text="M15 R_45 19:00 PUT\nM15 EURUSD 19:30 CALL",
        market="forex", day="today", timezone_name="America/Sao_Paulo",
        catalogue=CATALOGUE, now=datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc),
    )
    assert [x["symbol"] for x in result["valid"]] == ["frxEURUSD"]
    assert len(result["issues"]) == 1
    assert result["issues"][0]["code"] == "outside_market"
    assert "Derivados" in result["issues"][0]["reason"]
    assert result["valid"][0]["local_time"] == "19:30"
    assert result["valid"][0]["utc_offset"] == "UTC−03:00"


def test_malformed_unknown_duplicate_and_duration_validation_are_per_line():
    result = validate_signal_list(
        text="M5 EURUSD 19:30 BUY\nM5 NOASSET 19:31 PUT\nM5 EURUSD 19:32 PUT\nM5 EURUSD 19:32 PUT",
        market="forex", day="today", timezone_name="America/Sao_Paulo",
        catalogue=CATALOGUE, now=datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc),
    )
    assert len(result["valid"]) == 1
    assert [x["code"] for x in result["issues"]] == ["malformed", "unknown_asset", "duplicate"]


def test_signal_is_valid_only_inside_its_exact_entry_minute():
    before = validate_signal_list(
        text="M15 EURUSD 19:00 CALL", market="forex", day="today",
        timezone_name="America/Sao_Paulo", catalogue=CATALOGUE,
        now=datetime(2026, 11, 25, 21, 59, 59, tzinfo=timezone.utc),
    )
    at_boundary = validate_signal_list(
        text="M15 EURUSD 19:00 CALL", market="forex", day="today",
        timezone_name="America/Sao_Paulo", catalogue=CATALOGUE,
        now=datetime(2026, 11, 25, 22, 1, 0, tzinfo=timezone.utc),
    )
    assert len(before["valid"]) == 1
    assert at_boundary["valid"] == []
    assert at_boundary["issues"][0]["code"] == "past"


def test_today_and_tomorrow_use_selected_timezone_date():
    now = datetime(2026, 11, 26, 2, 30, tzinfo=timezone.utc)
    today = validate_signal_list(
        text="M1 EURUSD 22:00 CALL", market="forex", day="today",
        timezone_name="America/Sao_Paulo", catalogue=CATALOGUE, now=now,
    )
    tomorrow = validate_signal_list(
        text="M1 EURUSD 22:00 CALL", market="forex", day="tomorrow",
        timezone_name="America/Sao_Paulo", catalogue=CATALOGUE, now=now,
    )
    assert today["date"] == "2026-11-25"
    assert tomorrow["date"] == "2026-11-26"


def test_rejects_bad_request_wide_choices():
    with pytest.raises(ManualSignalInputError):
        validate_signal_list(text="", market="not-a-market", day="today",
                             timezone_name="UTC", catalogue=CATALOGUE)
    with pytest.raises(ManualSignalInputError):
        validate_signal_list(text="", market="forex", day="yesterday",
                             timezone_name="UTC", catalogue=CATALOGUE)
    with pytest.raises(ManualSignalInputError):
        validate_signal_list(text="", market="forex", day="today",
                             timezone_name="Europe/Paris", catalogue=CATALOGUE)


def test_limits_extreme_input_size_and_line_count():
    with pytest.raises(ManualSignalInputError):
        validate_signal_list(text="x" * 20_001, market="forex", day="today",
                             timezone_name="UTC", catalogue=CATALOGUE)
    with pytest.raises(ManualSignalInputError):
        validate_signal_list(text="\n".join("x" for _ in range(501)), market="forex",
                             day="today", timezone_name="UTC", catalogue=CATALOGUE)

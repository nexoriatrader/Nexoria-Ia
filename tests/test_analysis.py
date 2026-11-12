from app.analysis import analyze_prices


def test_analysis_waits_for_enough_history():
    result = analyze_prices([1.0] * 54)
    assert result["signal"] == "WAIT"
    assert result["indicators"] is None
    assert "54/55" in result["reason"]


def test_rising_breakout_is_explainable_and_informational():
    prices = [100.0 + i * 0.01 for i in range(60)]
    result = analyze_prices(prices)
    assert result["signal"] == "CALL"
    assert result["indicators"]["trend"] == "CALL"
    assert result["indicators"]["breakout"] == "CALL"
    assert result["informational_only"] is True
    assert "probability" not in result


def test_falling_breakout_is_explainable():
    prices = [100.0 - i * 0.01 for i in range(60)]
    result = analyze_prices(prices)
    assert result["signal"] == "PUT"
    assert result["indicators"]["trend"] == "PUT"
    assert result["indicators"]["breakout"] == "PUT"


def test_flat_series_does_not_invent_signal_or_volatility():
    result = analyze_prices([100.0] * 60)
    assert result["signal"] == "WAIT"
    assert result["indicators"]["rsi_14"] == 50.0
    assert result["indicators"]["volatility_pct"] == 0.0


def test_invalid_and_nonfinite_values_are_ignored():
    result = analyze_prices([None, "bad", float("nan"), float("inf"), *([10.0] * 55)])
    assert result["signal"] == "WAIT"
    assert result["samples"] == 55

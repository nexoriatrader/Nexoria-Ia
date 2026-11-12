import pytest
from app.autopilot import analyze, quote_assessment, wilson_lower_bound


def test_insufficient_data_is_not_a_trade_signal():
    x=analyze([100]*15)
    assert x['state']=='INSUFFICIENT_DATA' and x['candidates']==[]


def test_flat_series_produces_no_signal():
    assert analyze([100]*100)['candidates']==[]


def test_rising_prices_generate_hypotheses_not_validated_orders():
    x=analyze([100+i*.05 for i in range(100)])
    assert x['candidates']
    assert all(not c['validated'] for c in x['candidates'])
    assert all(c['direction'] in ('CALL','PUT') for c in x['candidates'])


def test_malformed_prices_are_rejected():
    with pytest.raises(ValueError):analyze([10]*79+[float('nan')])
    with pytest.raises(ValueError):analyze([10]*79+[-1])


def test_quote_breakeven_matches_stake_and_payout():
    x=quote_assessment('2','3.6')
    assert x['break_even_probability']==0.555556
    assert x['state']=='UNVALIDATED'


def test_sample_size_cannot_be_skipped():
    x=quote_assessment('2','3.6',wins=180,total=190)
    assert x['state']=='UNVALIDATED'


def test_positive_research_requires_large_strong_sample():
    assert quote_assessment('2','3.6',wins=850,total=1000)['state']=='RESEARCH_POSITIVE'
    assert quote_assessment('2','3.6',wins=520,total=1000)['state']=='RESEARCH_REJECTED'


def test_invalid_quote_and_sample():
    with pytest.raises(ValueError): quote_assessment('0','3')
    with pytest.raises(ValueError): quote_assessment('2','NaN')
    with pytest.raises(ValueError): wilson_lower_bound(11,10)

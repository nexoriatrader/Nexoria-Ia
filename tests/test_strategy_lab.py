"""Read-only strategy lab tests: broker data, chronology, and execution isolation."""
from datetime import timedelta
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db,main,strategy_lab
from app.market import MarketError
from app.security import encrypt,hashsecret


def candles(count=700):
    # Deterministic monotonic sample; never claims realistic profitability.
    return [{'epoch':1700000040+60*i,'close':100+i*0.1,
             'high':100+i*0.1+0.01,'low':100+i*0.1-0.01}
            for i in range(count)]


def test_families_filter_only_intended_indices():
    assert strategy_lab.family_of('Range Break 100 Index')=='range_break'
    assert strategy_lab.family_of('Range Break 200 Index')=='range_break'
    assert strategy_lab.family_of('Step Index')=='step'
    assert strategy_lab.family_of('Skew Step Index') is None
    assert strategy_lab.family_of('Multi Step Index') is None
    assert strategy_lab.family_of('Volatility 100 (1s)') is None


def test_no_lookahead_and_strict_split():
    d=candles()
    r=strategy_lab.evaluate(d,'range_break','RANGE_TEST','Range Break Test')
    assert r['orders_sent']==0
    assert r['payout_adjusted'] is False
    assert len(r['results'])==3
    assert [x['duration_minutes'] for x in r['results']]==[1,3,5]
    train_end=d[int(len(d)*.7)-1]['epoch']
    validate_start=d[int(len(d)*.7)]['epoch']
    for x in r['results']:
        if x['train']['signals']:
            assert x['train']['last_signal']<=train_end
        if x['validation']['signals']:
            assert x['validation']['first_signal']>=validate_start


def test_flat_market_does_not_invent_success_rate():
    d=[{'epoch':1700000040+60*i,'close':100.,'high':100.1,'low':99.9}
       for i in range(500)]
    for family in ('range_break','step'):
        r=strategy_lab.evaluate(d,family,'X_TEST','Test')
        for row in r['results']:
            assert row['train']['signals']==0
            assert row['validation']['hit_rate'] is None
            assert not row['enough_validation_samples']


def test_missing_candles_rejected():
    with pytest.raises(MarketError):
        strategy_lab.parse_candles({'candles':[]}) if False else strategy_lab.parse_candles({'error':'offline'})
    with pytest.raises(MarketError):
        strategy_lab.evaluate(candles(90),'step','STP','Step')


def test_research_never_submits_broker_buy(monkeypatch):
    monkeypatch.setattr(strategy_lab,'discover_symbols',AsyncMock(return_value=[
        {'symbol':'RANGE_TEST','display_name':'Range Break 100 Index'},
        {'symbol':'STEP_TEST','display_name':'Step Index'}]))
    monkeypatch.setattr(strategy_lab,'discover_contracts',AsyncMock(return_value=['CALL','PUT']))
    monkeypatch.setattr(strategy_lab,'fetch_candles',AsyncMock(return_value=candles()))
    monkeypatch.setattr(main,'demo_buy',AsyncMock(side_effect=AssertionError('Unexpected broker buy')))
    import asyncio
    result=asyncio.run(strategy_lab.run_lab())
    assert result['state']=='RESEARCH_ONLY'
    assert len(result['items'])==2
    assert result['orders_sent']==0
    main.demo_buy.assert_not_awaited()


def test_strategy_education_explains_indicators_without_guaranteeing_profits():
    from pathlib import Path
    html=(Path(__file__).resolve().parents[1]/'app'/'index.html').read_text()
    assert 'id="strategies"' in html
    assert 'Índice de Força Relativa' in html
    assert 'Média Móvel Exponencial' in html
    assert 'EMA 20' in html
    assert 'Indicadores e estratégias não garantem resultados' in html
    assert 'Operações financeiras envolvem riscos' in html


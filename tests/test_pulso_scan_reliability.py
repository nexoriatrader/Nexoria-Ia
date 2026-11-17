"""PULSO scan must not confuse temporary Deriv failures with a no-signal bar.

All tests are offline: no authenticated Deriv requests and no trading orders.
"""
import asyncio
from unittest.mock import AsyncMock

from app import pulso_live
from app.market import MarketError


def signal_for(symbol):
    return {
        'strategy':'test_original_signal','direction':'CALL','symbol':symbol,
        'minutes':5,'expiry_options':[5],'last_bar_epoch':60,
        'bar_seconds':60,'reason':'validated fake fixture','score':1,
    }


def test_pulso_retries_after_candle_transport_error_in_same_window(monkeypatch):
    """A failed Deriv fetch at M1 close must not suppress later valid data."""
    monkeypatch.setattr(pulso_live.time,'time',lambda:126.0)
    pulso_live._cache.clear()
    probe=AsyncMock(side_effect=[MarketError('temporary candles outage'),signal_for('R_100')])
    monkeypatch.setattr(pulso_live,'_candidate_for_symbol',probe)
    async def check():
        no_signal=await pulso_live.confirmed_pulso_signal(allowed_symbols=['R_100'])
        assert no_signal is None
        assert not pulso_live._cache, 'an API failure must never be memoized as no signal'
        repaired=await pulso_live.confirmed_pulso_signal(allowed_symbols=['R_100'])
        assert repaired is not None and repaired['symbol']=='R_100'
        assert probe.await_count==2
    try:
        asyncio.run(check())
    finally:
        pulso_live._cache.clear()


def test_pulso_keeps_fast_confirmed_candidate_when_other_market_times_out(monkeypatch):
    """One slow market cannot force abandonment of a separate validated candidate."""
    monkeypatch.setattr(pulso_live.time,'time',lambda:126.8)
    pulso_live._cache.clear()
    completed=[]
    async def broker(symbol,bucket,*,deadline,direction,minutes,indicators,verdict):
        if symbol=='R_FAST':
            return signal_for('R_FAST')
        try:
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            completed.append('slow-cancelled')
            raise
        return signal_for('R_SLOW')
    monkeypatch.setattr(pulso_live,'_candidate_for_symbol',broker)
    async def check():
        chosen=await pulso_live.confirmed_pulso_signal(
            allowed_symbols=['R_FAST','R_SLOW'])
        assert chosen is not None and chosen['symbol']=='R_FAST'
        assert completed==['slow-cancelled']
    try:
        asyncio.run(check())
    finally:
        pulso_live._cache.clear()


def test_pulso_caches_actual_no_signal_but_not_missing_closed_candle(monkeypatch):
    """A real no-signal verdict can be cached; missing M1 data is retried."""
    monkeypatch.setattr(pulso_live.time,'time',lambda:126.0)
    pulso_live._cache.clear()
    attempts=[]
    async def broker(symbol,bucket,*,deadline,direction,minutes,indicators,verdict):
        attempts.append(1)
        if len(attempts)==1:
            verdict('velas_insuficientes')
        else:
            verdict('estrategia_sem_sinal')
        return None
    monkeypatch.setattr(pulso_live,'_candidate_for_symbol',broker)
    async def check():
        assert await pulso_live.confirmed_pulso_signal(allowed_symbols=['R_100']) is None
        assert not pulso_live._cache
        assert await pulso_live.confirmed_pulso_signal(allowed_symbols=['R_100']) is None
        assert len(attempts)==2
        assert await pulso_live.confirmed_pulso_signal(allowed_symbols=['R_100']) is None
        assert len(attempts)==2
    try:
        asyncio.run(check())
    finally:
        pulso_live._cache.clear()

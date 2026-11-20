"""Research-only market data boundary for NEXORA Intelligence 2.0.

Reuse the existing bounded public candle client, in-flight deduplication,
optional shared cache, and closed-OHLC validation. This module creates NO
new WebSocket, table, scheduler, cache, API or financial execution path.
In particular, data quality NEVER grants trading permission.
"""
from __future__ import annotations

import math
import re
import time

from .. import market
from ..trade_selector_v2 import strict_closed_bars

SUPPORTED_GRANULARITIES = frozenset((60, 300, 900, 3600))
MAX_CANDLES = 1000
_SYMBOL = re.compile(r"[A-Za-z0-9_]{2,30}\Z")


async def read_closed_window(
    symbol: str, granularity_seconds: int, count: int, *,
    now: float | None = None, timeout: float = 8.0,
) -> dict:
    """Normalize a public, closed-candle window without inventing missing bars.

    The existing market.shared_candle_request remains the **only** upstream
    fetch/cache/deduplication path. strict_closed_bars supplies the existing
    OHLC, duplicate-epoch, finite-value and unfinished-candle safeguards.

    A stale, incomplete or insufficient window remains inspectable for
    diagnostics but is *not* usable for downstream research calculations.
    No output from this adapter is an order or execution authorization.
    """
    if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None:
        raise market.MarketError("Intelligence 2.0: símbolo público inválido")
    if type(granularity_seconds) is not int or granularity_seconds not in SUPPORTED_GRANULARITIES:
        raise market.MarketError("Intelligence 2.0: timeframe inválido")
    if type(count) is not int or not 2 <= count <= MAX_CANDLES:
        raise market.MarketError("Intelligence 2.0: quantidade de velas inválida")
    if isinstance(now, bool) or (
        now is not None and (
            not isinstance(now, (int, float)) or not math.isfinite(now) or now <= 0
        )
    ):
        raise market.MarketError("Intelligence 2.0: relógio inválido")
    if isinstance(timeout, bool) or (
        not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
        or not 0 < timeout <= 20
    ):
        raise market.MarketError("Intelligence 2.0: prazo de consulta inválido")

    observed_at = int(time.time() if now is None else now)
    payload = await market.shared_candle_request(
        symbol, granularity_seconds, count, timeout=timeout,
    )
    if not isinstance(payload, dict):
        raise market.MarketError("Intelligence 2.0: resposta de mercado inválida")

    # The authoritative validation is reused, not reimplemented.
    bars = strict_closed_bars(payload, granularity_seconds, now=observed_at)
    bars = bars[-count:]
    for bar in bars:
        if bar["epoch"] % granularity_seconds:
            raise market.MarketError("Intelligence 2.0: candle fora do timeframe")

    gaps = 0
    for left, right in zip(bars, bars[1:]):
        delta = right["epoch"] - left["epoch"]
        if delta <= 0 or delta % granularity_seconds:
            raise market.MarketError("Intelligence 2.0: timestamps incompatíveis")
        gaps += delta // granularity_seconds - 1

    expected_epoch = (observed_at // granularity_seconds - 1) * granularity_seconds
    last_epoch = bars[-1]["epoch"] if bars else None
    # Fail closed for any gap, closed-bar lag or insufficient data.
    if len(bars) < 2:
        quality = "INSUFFICIENT"
    elif gaps:
        quality = "GAPPED"
    elif last_epoch != expected_epoch:
        quality = "STALE"
    else:
        quality = "COMPLETE"

    return {
        "source": "existing_public_candle_feed",
        "symbol": symbol,
        "granularity_seconds": granularity_seconds,
        "observed_at_epoch": observed_at,
        "expected_last_closed_epoch": expected_epoch,
        "last_closed_epoch": last_epoch,
        "closed_count": len(bars),
        "missing_intervals": gaps,
        "quality": quality,
        "usable_for_research": quality == "COMPLETE",
        "candles": bars,
        "read_only": True,
        "execution_allowed": False,
    }

"""Non-executing, descriptive OHLC inspection inside the NEXORA application.

Uses the already available Deriv public candle client (including its bounded
shared cache). Does not inspect demo/real accounts, generate directions,
forecast outcomes or invoke any broker trading method. Historical values and
data quality have no relationship to execution authorizations.
"""
from __future__ import annotations

import hashlib
import json
import re

from .. import market
from .market_data import read_closed_window

ALLOWED_FRAMES = (60, 300, 900, 3600)
MAX_DISPLAY_CANDLES = 180
MIN_DISPLAY_CANDLES = 8
SYMBOL_FORMAT = re.compile(r"[A-Za-z0-9_]{2,30}\Z")


async def inspect_public_candles(*, symbol: str,
                                 granularity_seconds: int = 60,
                                 closed_candles: int = 60) -> dict:
    if not isinstance(symbol, str) or SYMBOL_FORMAT.fullmatch(symbol) is None:
        raise ValueError("Símbolo público inválido")
    if type(granularity_seconds) is not int or granularity_seconds not in ALLOWED_FRAMES:
        raise ValueError("Intervalo de velas não permitido")
    if (type(closed_candles) is not int or
            not MIN_DISPLAY_CANDLES <= closed_candles <= MAX_DISPLAY_CANDLES):
        raise ValueError("Quantidade de velas fora do limite")
    # Request an extra candle because the upstream latest bar may be unfinished.
    # Nothing missing is interpolated, substituted, or created.
    window = await read_closed_window(
        symbol, granularity_seconds, closed_candles + 1, timeout=8.0)
    bars = window["candles"][-closed_candles:]
    missing = sum(
        max(0, (b["epoch"] - a["epoch"]) // granularity_seconds - 1)
        for a, b in zip(bars, bars[1:])
    )
    quality = window["quality"]
    if quality == "STALE":
        # A delayed final candle must never appear current.
        quality = "STALE"
    elif missing > 0:
        quality = "GAPPED"
    elif len(bars) < closed_candles:
        quality = "INSUFFICIENT"
    elif quality == "INSUFFICIENT":
        # The requested N+1 upstream bars included an open candle, leaving
        # exactly N strictly closed bars. That is an adequate research window.
        quality = "COMPLETE" if not missing and (
            bars[-1]["epoch"] == window["expected_last_closed_epoch"]
        ) else "INSUFFICIENT"
    elif quality == "COMPLETE" and missing:
        quality = "GAPPED"
    eligible = quality == "COMPLETE" and len(bars) == closed_candles and missing == 0
    summary = None
    snapshot_sha256 = None
    if eligible:
        values = [float(x["close"]) for x in bars]
        summary = {
            "first_close": values[0],
            "last_close": values[-1],
            "minimum_close": min(values),
            "maximum_close": max(values),
            "first_closed_epoch": bars[0]["epoch"],
            "last_closed_epoch": bars[-1]["epoch"],
            "observed_candles": len(bars),
        }
        payload = json.dumps(bars, sort_keys=True, separators=(",", ":"),
                             allow_nan=False)
        snapshot_sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return {
        "report_kind": "PUBLIC_OHLC_DESCRIPTIVE_QUALITY_NOT_TRADING_SIGNALS",
        "origin_path": "DERIV_PUBLIC_CLIENT_OR_SHARED_PUBLIC_CACHE",
        "source_claim": "Deriv public candles via the existing NEXORA client",
        "source_independently_certified": False,
        "symbol": symbol,
        "granularity_seconds": granularity_seconds,
        "requested_closed_candles": closed_candles,
        "received_closed_candles": len(bars),
        "observed_at_epoch": window["observed_at_epoch"],
        "expected_last_closed_epoch": window["expected_last_closed_epoch"],
        "last_closed_epoch": window["last_closed_epoch"],
        "missing_intervals": missing,
        "quality": quality,
        "data_available": eligible,
        "descriptive_statistics": summary,
        "snapshot_sha256": snapshot_sha256,
        "read_only": True,
        "execution_allowed": False,
        "orders_sent": 0,
        "trade_recommendation": None,
        "predicted_outcome": None,
    }

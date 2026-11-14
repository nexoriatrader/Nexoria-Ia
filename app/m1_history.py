"""Read-only historical M1 collector for 1HZ100V.

Obtains original OHLC candles from the public Deriv history API (not
interpolated from M5), validates every minute and persists compressed UTC-day
chunks in the existing PostgreSQL database. Never uses authenticated sessions,
proposals, or trading commands.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import zlib
from datetime import datetime, timezone
from math import isfinite
from time import time

from sqlalchemy import Column, DateTime, Integer, String, Text, select

from . import db
from .market import MarketError, public_request

LOG = logging.getLogger("nexora.m1_history")
SYMBOL = "1HZ100V"
MINUTE = 60
DAYS = 30
PAGE_SIZE = 900
MAX_PAGES = 56


class M1HistoryDay(db.Base):
    __tablename__ = "historical_m1_days"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(30), nullable=False, index=True)
    utc_day = Column(String(10), nullable=False, index=True)
    compressed_candles = Column(Text, nullable=False)
    saved_at = Column(DateTime(timezone=True), nullable=False)


def _decode(blob: str) -> list:
    return json.loads(zlib.decompress(base64.b64decode(blob)).decode("utf-8"))


def _encode(items: list) -> str:
    return base64.b64encode(zlib.compress(
        json.dumps(items, separators=(",", ":"), allow_nan=False).encode("utf-8"),
        level=6
    )).decode("ascii")


def _read_existing() -> dict[int, list]:
    with db.SessionLocal() as session:
        rows = session.scalars(
            select(M1HistoryDay)
            .where(M1HistoryDay.symbol == SYMBOL)
            .order_by(M1HistoryDay.utc_day)
        ).all()
        candles = {}
        for row in rows:
            for item in _decode(row.compressed_candles):
                candles[int(item[0])] = item
        return candles


def _store(candles: dict[int, list], cutoff: int):
    days = {}
    for epoch, row in sorted(candles.items()):
        if epoch < cutoff:
            continue
        day = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")
        days.setdefault(day, []).append(row)
    with db.SessionLocal() as session:
        existing = session.scalars(
            select(M1HistoryDay).where(M1HistoryDay.symbol == SYMBOL)
        ).all()
        by_day = {r.utc_day: r for r in existing}
        now = datetime.now(timezone.utc)
        for day, rows in days.items():
            entity = by_day.pop(day, None)
            if entity is None:
                entity = M1HistoryDay(symbol=SYMBOL, utc_day=day,
                                      compressed_candles="", saved_at=now)
                session.add(entity)
            entity.compressed_candles = _encode(rows)
            entity.saved_at = now
        for old in by_day.values():
            session.delete(old)
        session.commit()


def history_status():
    candles = _read_existing()
    if not candles:
        return {"state": "PENDING", "symbol": SYMBOL, "candles": 0,
                "timeframe": "M1", "complete": False}
    epochs = sorted(candles)
    gaps = sum(b - a != MINUTE for a, b in zip(epochs, epochs[1:]))
    return {
        "state": "COMPLETE" if not gaps and len(epochs) >= 43100 else "PARTIAL",
        "symbol": SYMBOL,
        "timeframe": "M1",
        "candles": len(epochs),
        "first_utc": datetime.fromtimestamp(epochs[0], timezone.utc).isoformat(),
        "last_utc": datetime.fromtimestamp(epochs[-1], timezone.utc).isoformat(),
        "missing_intervals": gaps,
        "complete": gaps == 0 and len(epochs) >= 43100,
        "derived_timeframes_available": ["M3", "M5", "M15"] if gaps == 0 and len(epochs) >= 43100 else [],
        "source": "Deriv public ticks_history candles granularity=60",
        "orders_sent": 0
    }


async def collect_m1_30_days():
    """Fetch only the unknown tail when a previous continuous history exists."""
    now = int(time())
    last_closed = (now // MINUTE - 1) * MINUTE
    first = last_closed - DAYS * 86400 + MINUTE
    stored = await asyncio.to_thread(_read_existing)
    retained = {t: row for t, row in stored.items() if first <= t <= last_closed}
    # Reuse a contiguous previously persisted prefix, but always verify by
    # collecting back to the latest known candle and validating all 30 days.
    valid_prefix = bool(retained) and min(retained) <= first + MINUTE
    if valid_prefix:
        times = sorted(retained)
        valid_prefix = all(b - a == MINUTE for a, b in zip(times, times[1:]))
    stop_at = max(retained) if valid_prefix else first - MINUTE
    fetched = {}
    end = last_closed + MINUTE
    for page in range(MAX_PAGES):
        response = await public_request({
            "ticks_history": SYMBOL,
            "style": "candles",
            "granularity": MINUTE,
            "count": PAGE_SIZE,
            "end": str(end)
        }, expected="candles", timeout=25)
        raw = response.get("candles")
        if not isinstance(raw, list) or not raw:
            raise MarketError("Deriv did not return M1 candles")
        earliest = end
        for candle in raw:
            try:
                epoch = int(candle["epoch"])
                prices = [float(candle[key]) for key in ("open", "high", "low", "close")]
            except (KeyError, TypeError, ValueError, OverflowError):
                raise MarketError("Malformed M1 candle") from None
            # A response may contain an in-progress candle outside our closed window.
            # Discard it before validation; do not silently repair broker OHLC.
            if epoch > last_closed or epoch < first:
                earliest = min(earliest, epoch)
                continue
            if epoch % MINUTE:
                raise MarketError(f"M1 candle epoch misaligned: epoch={epoch}, remainder={epoch % MINUTE}")
            if not all(isfinite(p) and p > 0 for p in prices):
                raise MarketError(f"M1 candle nonpositive/nonfinite: epoch={epoch}")
            if not (prices[2] <= prices[0] <= prices[1] and
                    prices[2] <= prices[3] <= prices[1]):
                raise MarketError(f"M1 OHLC inconsistent: epoch={epoch}, o={prices[0]}, h={prices[1]}, l={prices[2]}, c={prices[3]}")
            earliest = min(earliest, epoch)
            if first <= epoch <= last_closed:
                fetched[epoch] = [epoch, *prices]
        if earliest <= stop_at:
            break
        if earliest >= end:
            raise MarketError("Historical pagination did not advance")
        # Deriv candle boundaries follow the requested end epoch. Advancing
        # by one second shifts all returned candles to :59 and corrupts M1.
        # Keep every pagination boundary aligned to a full UTC minute.
        end = earliest - MINUTE
        if page % 4 == 3:
            await asyncio.sleep(0.25)
    combined = {**retained, **fetched}
    expected = (last_closed - first) // MINUTE + 1
    if len(combined) != expected or min(combined, default=0) != first or max(combined, default=0) != last_closed:
        raise MarketError(f"Historical M1 coverage incomplete: {len(combined)}/{expected} minutes")
    if any(b - a != MINUTE for a, b in zip(sorted(combined), sorted(combined)[1:])):
        raise MarketError("M1 historical series contains gaps")
    await asyncio.to_thread(_store, combined, first)
    status = await asyncio.to_thread(history_status)
    LOG.warning("Research M1 1HZ100V complete: %s", json.dumps(status, separators=(",", ":")))
    return status


async def m1_collect_loop():
    await asyncio.sleep(100)
    while True:
        succeeded = False
        try:
            await asyncio.wait_for(collect_m1_30_days(), timeout=480)
            # Keep all market access read-only. Analysis runs on persisted M1
            # and uses no broker trading session or order placement calls.
            from .multi_timeframe_study import run_multitimeframe_backtest
            from .research_persistence import persist_multitimeframe_study
            report = await asyncio.wait_for(
                asyncio.to_thread(run_multitimeframe_backtest), timeout=180)
            report["generated_at"] = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(persist_multitimeframe_study, report)
            selected = report.get("training_selected") or {}
            succeeded = True
            LOG.warning("Research M1-M15 backtest complete: %s", json.dumps({
                "models_tested": report["models_tested"],
                "m1_candles": report["m1_candles"],
                "training_selected": selected,
                "exploratory_passes": sum(
                    bool(row["passed_exploratory_threshold"]) for row in report["results"]),
                "orders_sent": 0
            }, separators=(",", ":")))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("Research M1 1HZ100V unavailable: %s: %s",
                        type(exc).__name__, str(exc)[:140])
        # Retry missing/inconsistent public market data later, not once per day.
        # A successful dataset is refreshed daily; errors retry after 15 min.
        await asyncio.sleep(86400 if succeeded else 900)

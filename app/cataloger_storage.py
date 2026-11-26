"""Persistent shared cache for the Cataloger's public Deriv candles."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from . import db, signal_cataloger


HISTORY_DAYS = 60
SYNC_TTL_SECONDS = 600
RETENTION_SWEEP_INTERVAL_SECONDS = 3600
_storage_sync_lock = asyncio.Lock()
logger = logging.getLogger(__name__)


def _utc_datetime(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _insert_for(session):
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return pg_insert
    if dialect == "sqlite":
        return sqlite_insert
    raise RuntimeError(f"Banco não suportado para o cache do Catalogador: {dialect}")


def _ensure_locked_state(session, symbol: str, granularity_seconds: int):
    insert = _insert_for(session)
    session.execute(
        insert(db.CatalogerHistoryState)
        .values(
            symbol=symbol,
            granularity_seconds=granularity_seconds,
            synchronized_at=None,
            oldest_epoch=None,
            newest_epoch=None,
        )
        .on_conflict_do_nothing(
            index_elements=[
                db.CatalogerHistoryState.symbol,
                db.CatalogerHistoryState.granularity_seconds,
            ]
        )
    )
    return session.execute(
        select(db.CatalogerHistoryState)
        .where(
            db.CatalogerHistoryState.symbol == symbol,
            db.CatalogerHistoryState.granularity_seconds == granularity_seconds,
        )
        .with_for_update()
    ).scalar_one()


def _candle_record(symbol: str, granularity_seconds: int, candle: dict) -> dict:
    return {
        "symbol": symbol,
        "granularity_seconds": granularity_seconds,
        "epoch": int(candle["epoch"]),
        "open_price": repr(float(candle["open"])),
        "high_price": repr(float(candle["high"])) if candle.get("high") is not None else None,
        "low_price": repr(float(candle["low"])) if candle.get("low") is not None else None,
        "close_price": repr(float(candle["close"])),
    }


def _upsert_candles(session, records: list[dict]) -> None:
    if not records:
        return
    insert = _insert_for(session)
    model = db.CatalogerCandle
    conflict_columns = [model.symbol, model.granularity_seconds, model.epoch]
    for offset in range(0, len(records), 100):
        batch = records[offset:offset + 100]
        statement = insert(model).values(batch)
        session.execute(statement.on_conflict_do_update(
            index_elements=conflict_columns,
            set_={
                "open_price": statement.excluded.open_price,
                "high_price": statement.excluded.high_price,
                "low_price": statement.excluded.low_price,
                "close_price": statement.excluded.close_price,
            },
        ))


def _row_to_candle(row: db.CatalogerCandle) -> dict:
    result = {
        "epoch": row.epoch,
        "open": float(row.open_price),
        "close": float(row.close_price),
    }
    result["high"] = float(row.high_price) if row.high_price is not None else None
    result["low"] = float(row.low_price) if row.low_price is not None else None
    return result


def _prune_expired_candles(session, cutoff_epoch: int) -> None:
    session.execute(delete(db.CatalogerCandle).where(
        db.CatalogerCandle.epoch < cutoff_epoch,
    ))


async def prune_expired_candles(now_epoch: int | None = None) -> None:
    """Remove expired rows across every symbol and timeframe."""
    observed_now = int(datetime.now(timezone.utc).timestamp()) if now_epoch is None else int(now_epoch)
    cutoff = observed_now - HISTORY_DAYS * 86400
    async with _storage_sync_lock:
        session = db.SessionLocal()
        try:
            with session.begin():
                _prune_expired_candles(session, cutoff)
        finally:
            session.close()


async def retention_loop() -> None:
    """Periodically enforce the shared 60-day retention, including idle symbols."""
    while True:
        try:
            await prune_expired_candles()
        except Exception:
            logger.exception("Falha ao limpar candles expirados do Catalogador")
        await asyncio.sleep(RETENTION_SWEEP_INTERVAL_SECONDS)


async def fetch_historical_candles(*, symbol: str, granularity_seconds: int,
                                   lookback_days: int, now_epoch: int | None = None) -> list[dict]:
    """Return the requested slice, filling or incrementally refreshing a 60-day shared cache."""
    if type(lookback_days) is not int or lookback_days not in signal_cataloger.SUPPORTED_LOOKBACK_DAYS:
        raise ValueError("O histórico deve ser de 7, 15, 30 ou 60 dias.")
    if type(granularity_seconds) is not int or granularity_seconds not in signal_cataloger.SUPPORTED_GRANULARITIES:
        raise ValueError("Timeframe inválido para o Catalogador.")
    if not isinstance(symbol, str) or signal_cataloger._SYMBOL.fullmatch(symbol) is None:
        raise ValueError("Símbolo inválido para o Catalogador.")

    observed_now = int(datetime.now(timezone.utc).timestamp()) if now_epoch is None else int(now_epoch)
    storage_cutoff = observed_now - HISTORY_DAYS * 86400
    analysis_cutoff = observed_now - lookback_days * 86400

    # This process-local lock avoids duplicate work inside one API worker. The
    # state row lock below also serializes cache fills across Postgres workers.
    async with _storage_sync_lock:
        session = db.SessionLocal()
        try:
            with session.begin():
                state = _ensure_locked_state(session, symbol, granularity_seconds)
                _prune_expired_candles(session, storage_cutoff)
                stored = session.scalars(
                    select(db.CatalogerCandle).where(
                        db.CatalogerCandle.symbol == symbol,
                        db.CatalogerCandle.granularity_seconds == granularity_seconds,
                        db.CatalogerCandle.epoch >= storage_cutoff,
                        db.CatalogerCandle.epoch <= observed_now,
                    ).order_by(db.CatalogerCandle.epoch)
                ).all()
                sync_at = _as_utc(state.synchronized_at)
                cache_fresh = (
                    sync_at is not None
                    and (datetime.fromtimestamp(observed_now, timezone.utc) - sync_at).total_seconds()
                    < SYNC_TTL_SECONDS
                )

                merged = {row.epoch: _row_to_candle(row) for row in stored}
                if not cache_fresh:
                    if (sync_at is None or state.newest_epoch is None
                            or state.newest_epoch < storage_cutoff):
                        downloaded = await signal_cataloger.fetch_historical_candles(
                            symbol=symbol,
                            granularity_seconds=granularity_seconds,
                            lookback_days=HISTORY_DAYS,
                            now_epoch=observed_now,
                        )
                    else:
                        downloaded = await signal_cataloger.fetch_incremental_candles(
                            symbol=symbol,
                            granularity_seconds=granularity_seconds,
                            after_epoch=state.newest_epoch,
                            now_epoch=observed_now,
                        )
                    records = []
                    for candle in downloaded:
                        if storage_cutoff <= candle["epoch"] <= observed_now:
                            merged[candle["epoch"]] = candle
                            records.append(_candle_record(symbol, granularity_seconds, candle))
                    _upsert_candles(session, records)
                    state.synchronized_at = _utc_datetime(observed_now)
                    epochs = sorted(merged)
                    state.oldest_epoch = epochs[0] if epochs else None
                    state.newest_epoch = epochs[-1] if epochs else None

                result = [merged[epoch] for epoch in sorted(merged)
                          if analysis_cutoff <= epoch <= observed_now]
            return result
        finally:
            session.close()

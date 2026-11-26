import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import db, cataloger_storage as storage


@pytest.fixture
def cataloger_storage_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/cataloger-storage.sqlite")
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    yield factory
    engine.dispose()


def test_shared_cache_loads_sixty_days_once_reuses_short_window_and_updates_incrementally(
    cataloger_storage_db, monkeypatch
):
    factory = cataloger_storage_db
    now = (1_800_000_000 // 900) * 900
    initial_rows = [
        {"epoch": now - 10 * 86400, "open": 1.0, "high": 2.0, "low": 0.9, "close": 2.0},
        {"epoch": now - 5 * 86400, "open": 2.0, "high": 2.1, "low": 1.0, "close": 1.0},
        {"epoch": now - 900, "open": 1.0, "high": 1.2, "low": 0.8, "close": 1.1},
    ]
    newest_row = {"epoch": now, "open": 1.1, "high": 1.3, "low": 1.0, "close": 1.2}
    full_history = AsyncMock(return_value=initial_rows)
    incremental_history = AsyncMock(side_effect=[[newest_row], []])
    monkeypatch.setattr(storage.signal_cataloger, "fetch_historical_candles", full_history)
    monkeypatch.setattr(storage.signal_cataloger, "fetch_incremental_candles", incremental_history)

    async def scenario():
        # Even a short first analysis fills and persists the full 60-day window.
        first_short = await storage.fetch_historical_candles(
            symbol="frxEURUSD", granularity_seconds=900, lookback_days=7,
            now_epoch=now,
        )
        all_days_from_cache = await storage.fetch_historical_candles(
            symbol="frxEURUSD", granularity_seconds=900, lookback_days=60,
            now_epoch=now,
        )
        assert len(first_short) == 2
        assert len(all_days_from_cache) == 3
        assert full_history.await_count == 1
        assert full_history.await_args.kwargs["lookback_days"] == 60
        assert incremental_history.await_count == 0

        with factory() as session:
            session.add(db.CatalogerCandle(
                symbol="frxEURUSD", granularity_seconds=900,
                epoch=now - 61 * 86400, open_price="1", high_price="2",
                low_price="0", close_price="1",
            ))
            session.add(db.CatalogerCandle(
                symbol="R_25", granularity_seconds=300,
                epoch=now - 61 * 86400, open_price="1", high_price="2",
                low_price="0", close_price="1",
            ))
            session.commit()

        # Cleanup runs even while this key is fresh, and covers idle symbols.
        await storage.fetch_historical_candles(
            symbol="frxEURUSD", granularity_seconds=900, lookback_days=7,
            now_epoch=now,
        )
        with factory() as session:
            expired_rows = session.scalars(select(db.CatalogerCandle).where(
                db.CatalogerCandle.epoch < now - 60 * 86400,
            )).all()
        assert expired_rows == []
        assert full_history.await_count == 1
        assert incremental_history.await_count == 0

        refreshed = await storage.fetch_historical_candles(
            symbol="frxEURUSD", granularity_seconds=900, lookback_days=60,
            now_epoch=now + 1200,
        )
        assert refreshed[-1]["epoch"] == now
        assert incremental_history.await_count == 1
        assert incremental_history.await_args.kwargs["after_epoch"] == now - 900

        shorter_after_refresh = await storage.fetch_historical_candles(
            symbol="frxEURUSD", granularity_seconds=900, lookback_days=7,
            now_epoch=now + 1200,
        )
        assert len(shorter_after_refresh) == 3
        assert incremental_history.await_count == 1

        # A later sync prunes anything that aged out of the shared 60-day window.
        await storage.fetch_historical_candles(
            symbol="frxEURUSD", granularity_seconds=900, lookback_days=60,
            now_epoch=now + 1801,
        )
        with factory() as session:
            expired = session.scalar(select(db.CatalogerCandle).where(
                db.CatalogerCandle.symbol == "frxEURUSD",
                db.CatalogerCandle.granularity_seconds == 900,
                db.CatalogerCandle.epoch < now + 1801 - 60 * 86400,
            ))
        assert expired is None
        assert full_history.await_count == 1
        assert incremental_history.await_count == 2

    asyncio.run(scenario())

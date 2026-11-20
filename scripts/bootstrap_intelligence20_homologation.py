"""ONE-TIME operator-only initialization of a NEW isolated Stage 4 PG.

Never run in application startup. Requires exact host/database identity and
an empty PostgreSQL instance before ANY schema creation.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from sqlalchemy import inspect

from app import db
from app.research_persistence import HistoricalResearchReport  # register model
from app.intelligence2.stage4_homologation_service import isolated_database_guard

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
ORDER = (
    "F01_GLOBAL_FENCING_V1_POSTGRES.sql",
    "I04_KNOWLEDGE_BASE_V1_POSTGRES.sql",
    "I20_PUBLIC_RESEARCH_COORDINATION_V1_POSTGRES.sql",
)


def _run_sql(path: Path) -> None:
    raw = db.engine.raw_connection()
    try:
        with raw.cursor() as cursor:
            cursor.execute(path.read_text(encoding="utf-8"), prepare=False)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def initialize(*, confirm_fresh: bool = False) -> None:
    if not confirm_fresh:
        raise RuntimeError("Explicit fresh-isolated-PostgreSQL confirmation required")
    isolated_database_guard()
    with db.engine.connect() as connection:
        if inspect(connection).get_table_names():
            raise RuntimeError("Refusing to change a nonempty database")
    # Offline, operator-authorized schema creation ONLY in the isolated DB.
    db.Base.metadata.create_all(db.engine)
    for filename in ORDER:
        _run_sql(MIGRATIONS / filename)
    db.init_db()
    print("Isolated database initialized with formal F01 -> I04 -> coordination")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-fresh-isolated-homologation",
                        action="store_true")
    args = parser.parse_args()
    initialize(confirm_fresh=args.confirm_fresh_isolated_homologation)

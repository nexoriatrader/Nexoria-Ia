"""Offline historical OHLC quality verification for isolated homologation.

No brokerage integrations, market scanner, strategy code, user trading state,
signals, ranking, order management, network calls, or live price subscriptions.
An explicitly labeled deterministic historical fixture is not market evidence.
Only OHLC input quality and provenance are analyzed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Protocol, Sequence, Mapping

from sqlalchemy import (
    Column, Integer, String, DateTime, Text, Table, CheckConstraint,
    select, func,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from . import stage4_general_homologation as core

REVISION = "nexora_stage4_offline_ohlc_quality_v1"
FIXTURE = "REPRODUCIBLE_SYNTHETIC_OHLC_FIXTURE"
REFERENCE_ASOF = (1700000000 // 3600 + 1) * 3600
ALLOWED_GRANULARITIES = frozenset((60, 300, 900, 3600))

audits = Table(
    "nexora_stage4_offline_ohlc_audits", core.meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("snapshot_sha256", String(64), unique=True, nullable=False),
    Column("provider", String(56), nullable=False),
    Column("provenance", String(52), nullable=False),
    Column("as_of_epoch", Integer, nullable=False),
    Column("granularity_seconds", Integer, nullable=False),
    Column("bar_count", Integer, nullable=False),
    Column("quality", String(18), nullable=False),
    Column("gap_count", Integer, nullable=False),
    Column("reason_codes_json", Text, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("validated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("provenance = 'REPRODUCIBLE_SYNTHETIC_OHLC_FIXTURE'",
                    name="ck_stage4_offline_ohlc_source"),
    CheckConstraint("quality IN ('COMPLETE','INSUFFICIENT','STALE','GAPPED','INVALID')",
                    name="ck_stage4_offline_ohlc_quality"),
)

class HistoricalOhlcProvider(Protocol):
    """No networking contract: an external caller supplies a historical window."""
    provenance: str
    def load(self, *, granularity_seconds: int, count: int,
             as_of_epoch: int) -> Sequence[Mapping[str, int | float]]:
        ...

class DeterministicOhlcFixture:
    """Reproducible OHLC-shaped values. NOT prices obtained from a market."""
    provenance = FIXTURE
    def load(self, *, granularity_seconds: int, count: int,
             as_of_epoch: int) -> list[dict]:
        _params(granularity_seconds, count, as_of_epoch)
        first = (as_of_epoch // granularity_seconds - count) * granularity_seconds
        out = []
        for i in range(count):
            t = first + i * granularity_seconds
            # Time-based deterministic series with valid inter-bar continuity.
            # These figures are invented and must NEVER be presented as genuine prices.
            raw = 100 + 1.6 * math.sin(t / 90000) + 0.4 * math.cos(t / 33000)
            next_value = 100 + 1.6 * math.sin((t+granularity_seconds)/90000) + 0.4 * math.cos((t+granularity_seconds)/33000)
            opening, closing = round(raw, 6), round(next_value, 6)
            out.append({
                "epoch": t, "open": opening, "high": round(max(opening, closing)+.15, 6),
                "low": round(min(opening, closing)-.15, 6), "close": closing,
            })
        return out

class InMemoryOhlcArchive:
    """Interchangeable supplier of externally provided history; no I/O."""
    provenance = "CALLER_SUPPLIED_OFFLINE_OHLC"
    def __init__(self, candles: Sequence[Mapping[str, int | float]]):
        self._candles = [dict(b) for b in candles]
    def load(self, *, granularity_seconds: int, count: int,
             as_of_epoch: int) -> list[dict]:
        _params(granularity_seconds, count, as_of_epoch)
        return [dict(row) for row in self._candles][-count:]

def _params(granularity_seconds: int, count: int, as_of_epoch: int):
    if (type(granularity_seconds) is not int
            or granularity_seconds not in ALLOWED_GRANULARITIES):
        raise ValueError("Unsupported historical granularity")
    if type(count) is not int or not 2 <= count <= 1000:
        raise ValueError("Historical bar count out of bounds")
    if type(as_of_epoch) is not int or as_of_epoch <= granularity_seconds:
        raise ValueError("Invalid historical observation epoch")

def _digest(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         allow_nan=False, ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()

def inspect_window(provider: HistoricalOhlcProvider, *,
                   granularity_seconds: int = 3600,
                   count: int = 48,
                   as_of_epoch: int = REFERENCE_ASOF) -> dict:
    """Validate only data structure, closure, contiguity and snapshot freshness.

    No interpolation, forecast, ranking, market selection or execution advice.
    An archived sample may be COMPLETE at its historical reference time while
    being STALE compared with current time; both timestamps are disclosed.
    """
    _params(granularity_seconds, count, as_of_epoch)
    values = provider.load(granularity_seconds=granularity_seconds, count=count,
                           as_of_epoch=as_of_epoch)
    if not isinstance(values, (tuple, list)):
        raise ValueError("OHLC provider did not return a bounded sequence")
    if len(values) > count:
        raise ValueError("OHLC provider exceeded window limit")
    valid = []
    reasons: list[str] = []
    for source_row in values:
        if (not isinstance(source_row, Mapping)
                or set(source_row) != {"epoch","open","high","low","close"}):
            reasons.append("INVALID_FIELDS")
            break
        epoch = source_row.get("epoch")
        if type(epoch) is not int or epoch <= 0 or epoch % granularity_seconds:
            reasons.append("UNALIGNED_EPOCH")
            break
        nums = [source_row.get(k) for k in ("open","high","low","close")]
        if (any(type(v) not in (int,float) or not math.isfinite(v) or v <= 0
                for v in nums)
                or nums[2] > min(nums[0],nums[3])
                or nums[1] < max(nums[0],nums[3])
                or nums[1] < nums[2]):
            reasons.append("INVALID_OHLC")
            break
        if epoch + granularity_seconds > as_of_epoch:
            reasons.append("UNFINISHED_BAR")
            break
        valid.append({"epoch":epoch, "open":float(nums[0]),
                      "high":float(nums[1]), "low":float(nums[2]),
                      "close":float(nums[3])})
    gaps = 0
    if not reasons:
        for a,b in zip(valid,valid[1:]):
            delta=b["epoch"]-a["epoch"]
            if delta <= 0 or delta % granularity_seconds:
                reasons.append("NON_MONOTONIC_OR_DUPLICATE")
                break
            gaps += delta//granularity_seconds-1
    expected = (as_of_epoch // granularity_seconds-1)*granularity_seconds
    if reasons:
        quality = "INVALID"
    elif len(valid) < 2 or len(valid) != count:
        quality = "INSUFFICIENT"
        reasons.append("BAR_COUNT_TOO_LOW")
    elif gaps:
        quality = "GAPPED"
        reasons.append("MISSING_INTERVALS")
    elif valid[-1]["epoch"] != expected:
        quality = "STALE"
        reasons.append("LAST_BAR_NOT_CURRENT_AT_SNAPSHOT")
    else:
        quality = "COMPLETE"
        reasons.append("CLOSED_CONTIGUOUS_AT_HISTORICAL_SNAPSHOT")
    provenance = getattr(provider, "provenance", "UNKNOWN")
    if provenance not in (FIXTURE, "CALLER_SUPPLIED_OFFLINE_OHLC"):
        raise ValueError("Unknown OHLC provenance")
    if quality == "INVALID":
        # Never store or expose invalid bars as validated price observations.
        valid = []
    snapshot = {
        "provenance": provenance,
        "synthetic": provenance == FIXTURE,
        "historical_only": True,
        "granularity_seconds": granularity_seconds,
        "as_of_epoch": as_of_epoch,
        "expected_last_closed_epoch": expected,
        "bar_count": len(valid), "gap_count": gaps,
        "quality": quality, "reason_codes": reasons,
        "execution_allowed": False,
        "records": valid,
    }
    snapshot["sha256"] = _digest(snapshot)
    return snapshot

def record_fixture_audit(*, as_of_epoch: int = REFERENCE_ASOF) -> dict:
    """Idempotent insert into a homologation-only registered table."""
    snapshot = inspect_window(DeterministicOhlcFixture(), as_of_epoch=as_of_epoch)
    if snapshot["quality"] != "COMPLETE":
        raise RuntimeError("The historical fixture did not pass OHLC validation")
    record = {
        "snapshot_sha256":snapshot["sha256"],
        "provider":"DeterministicOhlcFixture",
        "provenance":FIXTURE,
        "as_of_epoch":snapshot["as_of_epoch"],
        "granularity_seconds":snapshot["granularity_seconds"],
        "bar_count":snapshot["bar_count"],"quality":snapshot["quality"],
        "gap_count":snapshot["gap_count"],
        "reason_codes_json":json.dumps(snapshot["reason_codes"]),
        "payload_json":json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
        "validated_at":datetime.now(timezone.utc),
    }
    with core._engine().begin() as connection:
        stmt = (pg_insert(audits) if connection.dialect.name=="postgresql"
                else sqlite_insert(audits) if connection.dialect.name=="sqlite"
                else None)
        if stmt is None:
            raise RuntimeError("Unsupported audit DB")
        inserted = connection.execute(stmt.values(**record)
                                      .on_conflict_do_nothing(
                                          index_elements=["snapshot_sha256"])
                                      .returning(audits.c.id)).scalar_one_or_none()
    return {"inserted":bool(inserted),"sha256":snapshot["sha256"],
            "quality":snapshot["quality"],"bar_count":snapshot["bar_count"],
            "historical_only":True,"synthetic":True}

def read_audits(limit: int = 10) -> dict:
    if type(limit) is not int or not 1 <= limit <= 30:
        raise ValueError("Invalid OHLC audit limit")
    with core._engine().connect() as connection:
        total = connection.execute(select(func.count()).select_from(audits)).scalar_one()
        rows = connection.execute(select(audits).order_by(
            audits.c.id.desc()).limit(limit)).mappings().all()
    output=[]
    for row in rows:
        snapshot=json.loads(row["payload_json"])
        digest=snapshot.pop("sha256",None)
        if not digest or digest != row["snapshot_sha256"] or digest != _digest(snapshot):
            raise RuntimeError("Stored OHLC audit failed content integrity")
        # Avoid displaying a large fabricated price series by default.
        output.append({
            "id":row["id"],"sha256":digest,
            "provenance":row["provenance"],
            "quality":row["quality"],"bar_count":row["bar_count"],
            "gap_count":row["gap_count"],
            "granularity_seconds":row["granularity_seconds"],
            "as_of_epoch":row["as_of_epoch"],
            "validated_at":row["validated_at"].isoformat(),
            "reasons":json.loads(row["reason_codes_json"]),
            "sample_first":snapshot["records"][0] if snapshot["records"] else None,
            "sample_last":snapshot["records"][-1] if snapshot["records"] else None,
            "synthetic":True,"historical_only":True,
        })
    return {"total":int(total),"audits":output,
            "source":FIXTURE,"synthetic":True,"historical_only":True,
            "i01_module_integrated":False,
            "market_feed_connected":False,
            "execution_allowed":False}

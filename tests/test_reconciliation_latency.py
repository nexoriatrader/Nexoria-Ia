"""DEMO recovery latency guardrails; no external buy calls are issued."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.main import _reconciliation_due


def order(*, age_seconds, minutes=15, state='CONFIRMED'):
    now=datetime(2026,11,16,18,0,tzinfo=timezone.utc)
    return SimpleNamespace(
        state=state,
        duration_minutes=minutes,
        created_at=now-timedelta(seconds=age_seconds),
        broker_purchase_at=None
    ), now


def test_far_from_expiry_does_not_reopen_broker_socket():
    obj,now=order(age_seconds=120,minutes=15)
    assert not _reconciliation_due(obj,now)


def test_near_expiry_is_eligible_to_reconcile_before_next_cycle():
    obj,now=order(age_seconds=898,minutes=15)
    assert _reconciliation_due(obj,now)


def test_unknown_contract_is_reconciled_immediately():
    obj,now=order(age_seconds=5,minutes=15,state='UNKNOWN')
    assert _reconciliation_due(obj,now)


def test_broker_purchase_time_takes_precedence_over_reservation_time():
    obj,now=order(age_seconds=1200,minutes=15)
    obj.broker_purchase_at=now-timedelta(seconds=200)
    assert not _reconciliation_due(obj,now)

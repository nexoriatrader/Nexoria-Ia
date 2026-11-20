"""Deterministic non-financial public research decisions and progressive read load.

500 requests are not 500 simultaneous network clients: this deliberately
measures only in-process concurrent public report aggregation. No broker,
orders, account identities, production backend or profitability claims.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import math
import os
import resource
import statistics
import time

import pytest

from app.intelligence2 import public_research_decisions as public
from app.intelligence2 import research_orchestrator

NOW = 20_040


def _report(*, valid=True, duplicate=False):
    asset = {
        "market": "forex", "symbol": "FRXEURUSD",
        "state": "RESEARCH_READY",
        "observed_trend": "UP", "context_consensus": "THREE_TIMEFRAMES",
        "evidence_strength": "CONSISTENT",
        "evidence_completeness_points": 100,
        "fingerprint": "a"*64 if valid else None,
        "valid_until_epoch": NOW+60 if valid else NOW-1,
        "reason_codes": ["PUBLIC_DATA_READY_FOR_STUDY"],
        "knowledge_base": {"status": "NOT_CONNECTED"},
        "learning_engine": {"status": "NOT_CONNECTED"},
        "execution_allowed": False, "approved_for_trading": False,
    }
    return {
        "version": research_orchestrator.VERSION,
        "state": "RESEARCH_CANDIDATES", "as_of_epoch": NOW,
        "assets": [asset, dict(asset)] if duplicate else [asset],
        "distribution": {"publish_allowed": False},
        "orders_sent": 0,
        "execution_allowed": False,
        "approved_for_trading": False,
    }


def test_prioritizes_complete_public_evidence_without_financial_recommendation():
    result = public.prioritize_public_research(_report(), now=NOW)
    assert result["count"] == 1
    assert result["items"][0]["state"] == "RESEARCH_READY"
    assert result["items"][0]["metric_kind"] == "DATA_COMPLETENESS_NOT_PROBABILITY"
    assert result["items"][0]["observed_public_trend"] == "UP"
    assert result["publish_allowed"] is False
    assert result["distribution_kind"] == "INTERNAL_SHARED_RESEARCH_SNAPSHOT_ONLY"
    assert result["orders_sent"] == 0
    assert not any(key in result["items"][0] for key in (
        "trade_side", "entry_epoch", "contract_expiry_minutes", "stake"
    ))


def test_expired_or_duplicate_research_never_published_as_ready():
    expired = public.prioritize_public_research(_report(valid=False), now=NOW)
    assert expired["items"][0]["state"] == "EXCLUDED"
    duplicate = public.prioritize_public_research(
        _report(duplicate=True), now=NOW,
    )
    assert duplicate["items"] == []


@pytest.mark.parametrize("key,value", [
    ("trade_side", "CALL"), ("entry_epoch", NOW),
    ("contract_expiry_minutes", 5), ("user_id", 112),
    ("account_id", "CR1234"), ("stake", "100"),
])
def test_rejects_actionable_or_private_public_research_payload(key, value):
    source = _report()
    source["assets"][0][key] = value
    assert public.prioritize_public_research(source, now=NOW)["items"] == []


@pytest.mark.parametrize("bad", [True, 0, -1, float("nan"), "8"])
def test_invalid_public_capacity_rejected(bad):
    with pytest.raises(ValueError):
        public.prioritize_public_research(_report(), now=NOW, max_items=bad)


@pytest.mark.parametrize("n,parallel", [
    (25, 4), (100, 8), (250, 12), (500, 16),
])
def test_progressive_simulated_read_subscribers(n, parallel):
    """Measures aggregator only; reports actual throughput/errors/latency.

    NOT a claim of 500 simultaneous authenticated users or backend workers.
    """
    report = _report()
    latencies = []
    errors = 0
    start_cpu = resource.getrusage(resource.RUSAGE_SELF)
    start = time.perf_counter()
    def consumer(index):
        before = time.perf_counter()
        result = public.prioritize_public_research(report, now=NOW)
        if result["items"][0]["state"] != "RESEARCH_READY":
            raise AssertionError("Unexpected public research state")
        return (time.perf_counter() - before)*1000
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = [pool.submit(consumer, i) for i in range(n)]
        for f in as_completed(futures):
            try:
                latencies.append(f.result())
            except Exception:
                errors += 1
    elapsed = max(1e-9, time.perf_counter()-start)
    before = start_cpu.ru_maxrss
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    ordered = sorted(latencies)
    p95 = ordered[max(0, math.ceil(.95*len(ordered))-1)] if ordered else None
    print(
        f"PUBLIC_RESEARCH_LOAD simulated_readers={n} "
        f"thread_workers={parallel} requests_per_second={n/elapsed:.1f} "
        f"p95_ms={p95:.3f} error_count={errors} "
        f"rss_peak_kb={after} rss_peak_delta_kb={max(0, after-before)}"
    )
    assert errors == 0
    assert len(latencies) == n
    assert p95 is not None

"""Independent, purely descriptive OHLC validation: no market or broker access."""
import pytest
from sqlalchemy import create_engine

from app.intelligence2 import stage4_ohlc_quality as lib
from app.intelligence2 import stage4_general_homologation as demo


def _snapshot():
    return lib.inspect_window(lib.DeterministicOhlcFixture())


def test_reproducible_fixture_passes_strict_historical_validation():
    first = _snapshot()
    second = _snapshot()
    assert first == second
    assert first["synthetic"] is True and first["historical_only"] is True
    assert first["quality"] == "COMPLETE"
    assert first["bar_count"] == 48
    assert first["gap_count"] == 0
    assert all(x["low"] <= min(x["open"],x["close"])
               <= max(x["open"],x["close"]) <= x["high"]
               for x in first["records"])


def test_provider_interchange_and_no_silent_interpolation():
    sample = _snapshot()
    good = lib.inspect_window(lib.InMemoryOhlcArchive(sample["records"]))
    assert good["quality"] == "COMPLETE"
    assert good["synthetic"] is False
    bars = list(sample["records"])
    bars.pop(4)
    observed = lib.inspect_window(lib.InMemoryOhlcArchive(bars))
    assert observed["quality"] in ("INSUFFICIENT","GAPPED")
    assert observed["gap_count"] >= 1


@pytest.mark.parametrize("mutation,expected",[
    (lambda b:b.append(dict(b[-1])),"INVALID"),
    (lambda b:b.__setitem__(0,{**b[0],"high":b[0]["low"]-.1}),"INVALID"),
    (lambda b:b.__setitem__(-1,{**b[-1],"epoch":b[-1]["epoch"]+3600}),"INVALID"),
])
def test_rejects_bad_duplicate_unfinished_or_invalid_ohlc(mutation, expected):
    bars=list(_snapshot()["records"])
    mutation(bars)
    result=lib.inspect_window(lib.InMemoryOhlcArchive(bars))
    assert result["quality"] == expected
    assert result["records"] == []


def test_idempotent_archive_and_restored_audit_after_engine_restart(monkeypatch,tmp_path):
    db_file=tmp_path/"ohlc.db"
    engine=create_engine("sqlite:///"+str(db_file))
    demo.meta.create_all(engine)
    monkeypatch.setattr(demo,"_engine",lambda:engine)
    first=lib.record_fixture_audit()
    second=lib.record_fixture_audit()
    assert first["inserted"] is True and second["inserted"] is False
    assert lib.read_audits()["total"] == 1
    engine.dispose()
    another=create_engine("sqlite:///"+str(db_file))
    monkeypatch.setattr(demo,"_engine",lambda:another)
    audit=lib.read_audits()
    assert audit["total"] == 1
    assert audit["audits"][0]["quality"] == "COMPLETE"
    assert audit["i01_module_integrated"] is False
    another.dispose()


def test_authenticated_ohlc_history_without_claiming_i01_integration(monkeypatch,tmp_path):
    from fastapi.testclient import TestClient
    engine=create_engine("sqlite:///"+str(tmp_path/"auth.db"))
    demo.meta.create_all(engine)
    monkeypatch.setattr(demo,"_engine",lambda:engine)
    monkeypatch.setenv("NEXORA_STAGE4_ACCESS_TOKEN","test-only-strong-secret-32-characters-long")
    lib.record_fixture_audit()
    c=TestClient(demo.app)
    assert c.get("/api/homologation/monitor/ohlc-quality").status_code == 401
    r=c.get("/api/homologation/monitor/ohlc-quality",
           headers={"X-NEXORA-STAGE4-KEY":"test-only-strong-secret-32-characters-long"})
    assert r.status_code == 200
    assert r.json()["total"] == 1
    assert r.json()["i01_module_integrated"] is False
    assert r.json()["market_feed_connected"] is False
    assert r.json()["execution_allowed"] is False
    engine.dispose()

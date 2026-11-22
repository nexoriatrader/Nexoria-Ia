"""Private, read-only research evidence export: authenticated, isolated and verifiable."""
import json
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import db, main
from app.intelligence2.research_export import export_report, verify_export_checksums
from app.intelligence2.historical_research import build_report
from app.research_persistence import HistoricalResearchReport
from app.security import encrypt, hashsecret


def _fixture(symbol="EXPORT_TEST"):
    # SYNTHETIC TEST DATA ONLY; never represented as broker-market data.
    start = 1_700_000_100 // 900 * 900
    candles = []
    for i in range(1140):
        opening = 100.0 + i * 0.003
        closing = opening + (0.02 if i % 4 else -0.01)
        candles.append({
            "epoch": start + i * 60, "open": opening,
            "high": max(opening, closing) + 0.05,
            "low": min(opening, closing) - 0.05, "close": closing,
        })
    return {"symbol": symbol, "granularity_seconds": 60, "candles": candles}


def _sessions(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/research-export.sqlite")
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    with factory() as session:
        first = db.User(deriv_account_id="V-EXPORT-USER-ONE")
        second = db.User(deriv_account_id="V-EXPORT-USER-TWO")
        session.add_all([first, second])
        session.flush()
        for token, user in (("first", first), ("second", second)):
            session.add(db.Session(
                session_hash=hashsecret("export-" + token),
                user_id=user.id,
                token_encrypted=encrypt("dummy-test-token"),
                expires_at=db.utcnow() + timedelta(days=1),
            ))
        session.commit()
    return engine, factory


def test_export_owner_only_no_ddl_and_no_financial_data(tmp_path, monkeypatch):
    engine, factory = _sessions(tmp_path, monkeypatch)
    try:
        anonymous = TestClient(main.app)
        first = TestClient(main.app)
        second = TestClient(main.app)
        first.cookies.set("nexora_sid", "export-first")
        second.cookies.set("nexora_sid", "export-second")
        route = "/api/nexora2/ohlc/research"
        assert anonymous.get(route + "/1/export").status_code == 401
        created = first.post(route, json=_fixture())
        assert created.status_code == 200, created.text
        report_id = created.json()["id"]
        download_url = f"{route}/{report_id}/export"

        assert second.get(download_url).status_code == 404
        statements = []
        def watch(conn, cursor, statement, params, context, executemany):
            statements.append(statement.strip().split(None, 1)[0].upper())
        event.listen(engine, "before_cursor_execute", watch)
        try:
            exported = first.get(download_url)
        finally:
            event.remove(engine, "before_cursor_execute", watch)

        assert exported.status_code == 200, exported.text
        assert exported.headers["content-type"].startswith("application/json")
        assert exported.headers["content-disposition"] == (
            f'attachment; filename="nexora-pesquisa-{report_id}.json"'
        )
        assert "no-store" in exported.headers["cache-control"]
        assert exported.headers["x-content-type-options"] == "nosniff"
        assert statements and set(statements) == {"SELECT"}, statements

        exported_json = exported.json()
        assert exported_json["format"] == "NEXORA_2_RESEARCH_EXPORT_V1"
        assert exported_json["scope"] == "OWN_UNVERIFIED_HISTORICAL_RESEARCH"
        assert exported_json["study_id"] == report_id
        assert exported_json["report"]["source"] == "USER_SUPPLIED_UNVERIFIED_OHLC"
        assert exported_json["report"]["evidence"]["market_source_verified"] is False
        assert exported_json["report"]["orders_sent"] == 0
        assert exported_json["report"]["execution_allowed"] is False
        assert exported_json["input"]["symbol"] == "EXPORT_TEST"
        assert len(exported_json["input"]["candles"]) == 1140
        assert len(exported_json["report"]["evidence"]["input_sha256"]) == 64
        assert len(exported_json["report"]["evidence"]["result_sha256"]) == 64
        assert set(exported_json["report"]["contexts"]) == {"higher", "intermediate", "lower"}
        assert exported_json["report"]["contexts"]["lower"]["evidence"]["closed_bars_used"] == 72
        assert "ema20" in exported_json["report"]["contexts"]["lower"]["evidence"]
        assert "rsi14" in exported_json["report"]["contexts"]["lower"]["evidence"]
        assert "contexts" in exported_json["report"]["timeframe_alignment"]
        assert len(exported_json["report"]["orchestrator"]["stages"]) == 8
        assert verify_export_checksums(exported_json) == {
            "internal_checksums_match": True,
            "market_source_verified": False,
            "source": "USER_SUPPLIED_UNVERIFIED_OHLC",
            "version": "nexora2_uploaded_ohlc_v1",
            "input_count": 1140,
        }
        assert "token_encrypted" not in exported.text
        assert "deriv_account_id" not in exported.text
        assert "dummy-test-token" not in exported.text
        assert second.get(route + "/0/export").status_code == 404
    finally:
        engine.dispose()


def test_corrupted_saved_evidence_cannot_be_exported(tmp_path, monkeypatch):
    engine, factory = _sessions(tmp_path, monkeypatch)
    try:
        client = TestClient(main.app)
        client.cookies.set("nexora_sid", "export-first")
        route = "/api/nexora2/ohlc/research"
        created = client.post(route, json=_fixture())
        assert created.status_code == 200, created.text
        report_id = created.json()["id"]
        with factory() as session:
            row = session.get(HistoricalResearchReport, report_id)
            body = json.loads(row.payload_json)
            body["statistics"]["up_intervals"] += 1
            row.payload_json = json.dumps(body)
            session.commit()
        result = client.get(f"{route}/{report_id}/export")
        assert result.status_code == 409
        assert "Resultado histórico alterado" in result.json()["detail"]
        assert "content-disposition" not in result.headers
    finally:
        engine.dispose()


def test_saved_history_has_per_report_export_link_and_no_broker_routes():
    page = (Path(__file__).resolve().parents[1] / "app" / "nexora2.html").read_text()
    assert "BAIXAR RELATÓRIO JSON" in page
    assert "'/api/nexora2/ohlc/research/'+encodeURIComponent(item.id)+'/export'" in page
    assert "ohlc-export-link" in page
    for forbidden in ("/api/real/buy", "/api/demo/buy", "/api/autopilot/start"):
        assert forbidden not in page


def test_export_is_full_versioned_analytics_and_validatable_without_db():
    report = build_report(_fixture("OFFLINE_CHECK"))
    bundle = export_report(report, 93)
    assert bundle["study_id"] == 93
    assert "input" not in bundle["report"]
    assert bundle["input"] == report["input"]
    assert bundle["report"] == {
        name: value for name, value in report.items() if name != "input"
    }
    assert verify_export_checksums(bundle)["internal_checksums_match"] is True
    # A JSON roundtrip is the format used for downloads.
    replayed = json.loads(json.dumps(bundle, allow_nan=False))
    assert verify_export_checksums(replayed)["internal_checksums_match"] is True
    assert replayed["report"]["models"]["status"] == "NO_VALIDATED_MODEL_AVAILABLE"
    assert replayed["report"]["contexts"]["higher"]["source"] == (
        "USER_SUPPLIED_UNVERIFIED_OHLC"
    )
    assert replayed["report"]["timeframe_alignment"]["approved_for_trading"] is False


def test_export_verifier_rejects_input_and_statistical_tampering():
    import copy
    import pytest
    original = export_report(build_report(_fixture("TAMPER_CHECK")), 99)
    bad_input = copy.deepcopy(original)
    bad_input["input"]["candles"][0]["close"] += .01
    with pytest.raises(ValueError, match="entrada histórica"):
        verify_export_checksums(bad_input)
    bad_result = copy.deepcopy(original)
    bad_result["report"]["statistics"]["up_intervals"] += 1
    with pytest.raises(ValueError, match="resultado histórico"):
        verify_export_checksums(bad_result)
    bad_context = copy.deepcopy(original)
    bad_context["report"]["contexts"]["higher"]["evidence"]["ema20"] = 999
    with pytest.raises(ValueError, match="resultado histórico"):
        verify_export_checksums(bad_context)
    bad_source = copy.deepcopy(original)
    bad_source["report"]["source"] = "VERIFIED_BROKER_DATA"
    with pytest.raises(ValueError, match="Metadados"):
        verify_export_checksums(bad_source)


def test_export_verifier_rejects_missing_unsafe_or_non_integer_ids():
    import copy
    import pytest
    original = export_report(build_report(_fixture("CHECK_META")), 77)
    for invalid in (None, False, 0, -1, "77"):
        bad = copy.deepcopy(original)
        bad["study_id"] = invalid
        with pytest.raises(ValueError):
            verify_export_checksums(bad)
    bad = copy.deepcopy(original)
    bad["scope"] = "GLOBAL"
    with pytest.raises(ValueError, match="escopo"):
        verify_export_checksums(bad)
    bad = copy.deepcopy(original)
    bad["report"]["orders_sent"] = 1
    with pytest.raises(ValueError, match="Metadados"):
        verify_export_checksums(bad)


def test_standalone_export_checksum_cli_runs_without_database_or_broker(
        tmp_path):
    import subprocess
    import sys
    from app.intelligence2.research_export import verify_export_checksums

    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_nexora2_export.py"
    study = export_report(build_report(_fixture("STANDALONE_CHECK")), 15)
    source = tmp_path / "complete-evidence.json"
    source.write_text(json.dumps(study, allow_nan=False), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), str(source)],
        capture_output=True, text=True, timeout=15,
        env={"PATH": "", "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed == verify_export_checksums(study)

    changed = json.loads(source.read_text(encoding="utf-8"))
    changed["report"]["contexts"]["lower"]["evidence"]["rsi14"] += .5
    source.write_text(json.dumps(changed, allow_nan=False), encoding="utf-8")
    bad = subprocess.run(
        [sys.executable, str(script), str(source)],
        capture_output=True, text=True, timeout=15,
        env={"PATH": "", "PYTHONIOENCODING": "utf-8"},
    )
    assert bad.returncode == 1
    assert "Checksum do relatório analítico" in bad.stderr
    assert "rsi14" not in bad.stderr
    assert "STANDALONE_CHECK" not in bad.stderr


def test_standalone_validator_rejects_duplicate_json_keys_and_truncated_input(
        tmp_path):
    import subprocess
    import sys
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_nexora2_export.py"
    for idx, contents in enumerate((
        '{"study_id":1,"study_id":2}',
        '{"format":"NEXORA_2_RESEARCH_EXPORT_V1","input":',
        '{"nan": NaN}',
    )):
        path = tmp_path / f"malformed-{idx}.json"
        path.write_text(contents, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(script), str(path)],
            capture_output=True, text=True, timeout=15,
            env={"PATH": "", "PYTHONIOENCODING": "utf-8"},
        )
        assert result.returncode == 1
        assert "INVALID:" in result.stderr

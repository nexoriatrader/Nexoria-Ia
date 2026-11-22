"""Private, read-only research evidence export: authenticated, isolated and verifiable."""
import json
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import db, main
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

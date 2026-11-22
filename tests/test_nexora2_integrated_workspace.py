"""Integration safety contract across NEXORA 2.0 research features.

This test never connects to a broker. It uses a disposable SQLite database,
dummy authenticated sessions and clearly synthetic test candles.
"""
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import db, main
from app.nexora2_personal_history import personal_research_key
from app.research_persistence import HistoricalResearchReport, RESEARCH_KEY
from app.security import encrypt, hashsecret


def _test_ohlc(symbol, drift):
    first = 1_700_000_100 // 900 * 900
    candles = []
    for i in range(1140):
        value = 100.0 + i * drift
        close = value + (0.02 if i % 3 else -0.01)
        candles.append({
            "epoch": first + 60 * i,
            "open": value,
            "high": max(value, close) + 0.05,
            "low": min(value, close) - 0.05,
            "close": close,
        })
    return {"symbol": symbol, "granularity_seconds": 60, "candles": candles}


def test_integrated_private_studies_and_legacy_research_coexist(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/integrated.sqlite")
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    try:
        with sessions() as session:
            a = db.User(deriv_account_id="V-INTEGRATED-A")
            b = db.User(deriv_account_id="V-INTEGRATED-B")
            session.add_all([a, b])
            session.flush()
            owner_id = a.id
            for token, user in (("a1", a), ("a2", a), ("b1", b)):
                session.add(db.Session(
                    session_hash=hashsecret("integration-" + token),
                    user_id=user.id,
                    token_encrypted=encrypt("dummy-test-token"),
                    expires_at=db.utcnow() + timedelta(days=2),
                ))
            now=db.utcnow()
            session.add(HistoricalResearchReport(
                research_key=RESEARCH_KEY,
                created_at=now, period_from=now, period_to=now,
                payload_json='{"verified":"legacy research record unchanged"}',
            ))
            session.commit()

        client = TestClient(main.app)
        assert client.get("/api/nexora2/research/history").status_code == 401
        assert client.get("/api/nexora2/ohlc/research").status_code == 401
        client.cookies.set("nexora_sid", "integration-a1")
        visit = client.post("/api/nexora2/research/history", json={
            "kind": "intelligence",
        })
        assert visit.status_code == 200, visit.text
        assert visit.json()["recorded"] is True

        first = client.post("/api/nexora2/ohlc/research",
                            json=_test_ohlc("INTEGRATED_A", .002))
        second = client.post("/api/nexora2/ohlc/research",
                             json=_test_ohlc("INTEGRATED_B", .005))
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        first_id, second_id = first.json()["id"], second.json()["id"]
        assert first_id != second_id
        assert first.json()["report"]["source"] == "USER_SUPPLIED_UNVERIFIED_OHLC"
        assert len(client.get("/api/nexora2/research/history").json()["items"]) == 1
        assert len(client.get("/api/nexora2/ohlc/research").json()["items"]) == 2

        comparison = client.get(
            f"/api/nexora2/ohlc/research/compare?left_id={first_id}&right_id={second_id}"
        )
        assert comparison.status_code == 200, comparison.text
        assert comparison.json()["source"] == "USER_SUPPLIED_UNVERIFIED_OHLC"
        assert comparison.json()["orders_sent"] == 0
        assert comparison.json()["execution_allowed"] is False

        second_browser = TestClient(main.app)
        second_browser.cookies.set("nexora_sid", "integration-a2")
        assert len(second_browser.get("/api/nexora2/research/history").json()["items"]) == 1
        assert len(second_browser.get("/api/nexora2/ohlc/research").json()["items"]) == 2
        replay = second_browser.post(
            f"/api/nexora2/ohlc/research/{first_id}/replay"
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["reproduced_identically"] is True

        foreign = TestClient(main.app)
        foreign.cookies.set("nexora_sid", "integration-b1")
        assert foreign.get("/api/nexora2/research/history").json()["items"] == []
        assert foreign.get("/api/nexora2/ohlc/research").json()["items"] == []
        assert foreign.get(f"/api/nexora2/ohlc/research/{first_id}").status_code == 404
        assert foreign.get(
            f"/api/nexora2/ohlc/research/compare?left_id={first_id}&right_id={second_id}"
        ).status_code == 404

        with sessions() as session:
            assert session.scalar(select(HistoricalResearchReport).where(
                HistoricalResearchReport.research_key == RESEARCH_KEY
            )).payload_json == '{"verified":"legacy research record unchanged"}'
            assert session.scalar(select(HistoricalResearchReport).where(
                HistoricalResearchReport.research_key == personal_research_key(owner_id)
            )) is not None
    finally:
        engine.dispose()


def test_modern_dashboard_and_complete_research_workspace_markup():
    root=Path(__file__).resolve().parents[1]/"app"
    dashboard=(root/"index.html").read_text(encoding="utf-8")
    workspace=(root/"nexora2.html").read_text(encoding="utf-8")
    for identifier in ("robotButton", "robotState", "dashAccountMode",
                       "dashServerRobotState"):
        assert dashboard.count(f'id="{identifier}"') == 1
    assert 'onclick="toggleRobot()"' in dashboard
    assert "robotRunning=Boolean(status.running)" in dashboard
    for identifier in ("personalResearchHistory", "ohlcRunButton", "ohlcCompareButton",
                       "ohlcHistoryList", "persistedResearchLibrary",
                       "tabIntelligence", "tabSignals", "tabStrategies"):
        assert workspace.count(f'id="{identifier}"') == 1
    assert "savePersonalResearchVisit('intelligence')" in workspace
    assert "savePersonalResearchVisit('strategies')" in workspace
    assert "compareUploadedOhlcReports()" in workspace
    assert "Robô DEMO existente" in workspace
    for forbidden in ("/api/real/buy", "/api/demo/buy", "/api/autopilot/start"):
        assert forbidden not in workspace

"""End-to-end isolated uploaded OHLC HTTP + persistence, never any broker calls."""
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
import pytest

from app.intelligence2 import stage4_general_homologation as core
from app.intelligence2 import stage4_ohlc_quality as ohlc
from app.intelligence2 import stage4_user_ohlc as uploads
from app.intelligence2 import stage4_historical_research as research

KEY = "local-isolated-stage4-test-key-" + "x" * 34
HEADERS = {"X-NEXORA-STAGE4-KEY": KEY}


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "research.sqlite"),
                           connect_args={"check_same_thread":False})
    core.meta.create_all(engine)
    monkeypatch.setattr(core, "_engine", lambda: engine)
    monkeypatch.setenv("NEXORA_STAGE4_ACCESS_TOKEN", KEY)
    yield engine
    engine.dispose()


def sample(origin="SYNTHETIC_TEST"):
    bars = ohlc.DeterministicOhlcFixture().load(
        granularity_seconds=300, count=96, as_of_epoch=ohlc.REFERENCE_ASOF)
    return {
        "symbol":"OFFLINE_FIXTURE_A", "asset_class":"OTHER",
        "origin_type":origin, "source_label":"Offline test fixture only",
        "granularity_seconds":300, "bars":bars,
    }


def test_uploaded_asset_and_seven_stage_research_survive_reconnection(
        isolated_db, monkeypatch, tmp_path):
    client = TestClient(core.app)  # no lifespan, no ingest-loop
    assert client.post("/api/homologation/assets",json=sample()).status_code==401
    assert client.get("/api/homologation/assets").status_code==401
    assert client.post("/api/homologation/research/runs/uploaded/"+"a"*32).status_code==401
    uploaded=client.post("/api/homologation/assets",json=sample(),headers=HEADERS)
    assert uploaded.status_code==200,uploaded.text
    meta=uploaded.json()
    assert meta["quality"]=="COMPLETE" and meta["bar_count"]==96
    assert meta["synthetic"] is True and meta["provenance_verified"] is False
    duplicate=client.post("/api/homologation/assets",json=sample(),headers=HEADERS)
    assert duplicate.json()["already_present"] is True
    assert duplicate.json()["id"]==meta["id"]
    catalog=client.get("/api/homologation/assets",headers=HEADERS)
    assert catalog.status_code==200 and catalog.json()["total"]==1
    record=client.get("/api/homologation/assets/"+meta["id"],headers=HEADERS)
    assert record.status_code==200 and len(record.json()["bars"])==96
    response=client.post("/api/homologation/research/runs/uploaded/"+meta["id"],
                         headers=HEADERS)
    assert response.status_code==200,response.text
    run=response.json()
    assert run["state"]=="COMPLETE" and run["step_count"]==7
    detail=client.get("/api/homologation/research/runs/"+run["id"],headers=HEADERS)
    assert detail.status_code==200,detail.text
    report=detail.json()
    assert report["result_sha256"]==run["result_sha256"]
    assert [item["name"] for item in report["stages"]]==list(research.STAGES)
    assert report["report"]["dataset_id"]==meta["id"]
    assert report["report"]["provenance_verified"] is False
    assert report["report"]["execution_allowed"] is False
    assert report["report"]["trade_recommendation"] is None
    assert report["report"]["historical_validation"]["holdout_observations"]>10
    runs=client.get("/api/homologation/research/runs",headers=HEADERS)
    assert runs.status_code==200 and runs.json()["total"]==1
    assert runs.json()["runs"][0]["synthetic"] is True

    # Simulate service worker restart: discard connection pool and reopen same DB.
    isolated_db.dispose()
    replacement=create_engine("sqlite:///" + str(tmp_path/"research.sqlite"),
                              connect_args={"check_same_thread":False})
    monkeypatch.setattr(core,"_engine",lambda:replacement)
    try:
        assert client.get("/api/homologation/assets",headers=HEADERS).json()["total"]==1
        restored=client.get("/api/homologation/research/runs/"+run["id"],
                            headers=HEADERS)
        assert restored.status_code==200
        assert restored.json()["result_sha256"]==run["result_sha256"]
        assert len(restored.json()["stages"])==7
    finally:
        replacement.dispose()


def test_invalid_bars_are_rejected_without_persistence(isolated_db):
    client=TestClient(core.app)
    payload=sample()
    payload["bars"][0]["high"]=-1.0
    result=client.post("/api/homologation/assets",json=payload,headers=HEADERS)
    assert result.status_code==422,result.text
    assert client.get("/api/homologation/assets",headers=HEADERS).json()["total"]==0
    assert client.post("/api/homologation/research/runs/uploaded/"+"0"*32,
                       headers=HEADERS).status_code==404
    assert not any("/buy" in route.path or "/orders" in route.path
                   for route in core.app.routes)


def test_origin_markers_cannot_claim_verified_prices(isolated_db):
    client=TestClient(core.app)
    payload=sample("DECLARED_REAL_UNVERIFIED")
    created=client.post("/api/homologation/assets",json=payload,headers=HEADERS)
    assert created.status_code==200
    assert created.json()["synthetic"] is False
    assert created.json()["provenance_verified"] is False
    started=client.post("/api/homologation/research/runs/uploaded/"+
                        created.json()["id"],headers=HEADERS)
    assert started.status_code==200,started.text
    stored=client.get("/api/homologation/research/runs/"+started.json()["id"],
                      headers=HEADERS).json()
    assert stored["synthetic"] is False
    assert stored["report"]["origin_type"]=="DECLARED_REAL_UNVERIFIED"
    assert stored["report"]["provenance_verified"] is False
    assert "No signals, orders or trading recommendations" in stored["report"]["limitations"]


def test_exported_research_evidence_is_authenticated_and_read_only(isolated_db):
    client = TestClient(core.app)  # isolated API; no broker or startup loop
    page = client.get("/")
    assert page.status_code == 200
    assert "Baixar evidências (JSON)" in page.text
    assert "researchDetailRequest" in page.text
    assert "URL.revokeObjectURL" in page.text

    uploaded = client.post("/api/homologation/assets", json=sample(),
                           headers=HEADERS)
    assert uploaded.status_code == 200, uploaded.text
    started = client.post(
        "/api/homologation/research/runs/uploaded/" + uploaded.json()["id"],
        headers=HEADERS,
    )
    assert started.status_code == 200, started.text
    run_id = started.json()["id"]
    path = "/api/homologation/research/runs/" + run_id + "/export"

    assert client.get(path).status_code == 401
    assert client.get("/api/homologation/research/runs/not-an-id/export",
                      headers=HEADERS).status_code == 422
    assert client.get("/api/homologation/research/runs/" + "0" * 32 + "/export",
                      headers=HEADERS).status_code == 404

    before = client.get("/api/homologation/research/runs",
                        headers=HEADERS).json()["total"]
    response = client.get(path, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == (
        'attachment; filename="nexora-research-' + run_id + '.json"'
    )
    exported = response.json()
    assert exported["format_version"] == "nexora-historical-research-evidence-v1"
    assert exported["provenance_verified"] is False
    assert exported["financial_execution_allowed"] is False
    assert exported["research"]["id"] == run_id
    assert exported["research"]["result_sha256"] == started.json()["result_sha256"]
    assert exported["research"]["report"]["provenance_verified"] is False
    assert len(exported["research"]["stages"]) == len(research.STAGES)
    assert KEY not in response.text
    assert client.get(path, headers=HEADERS).json() == exported
    assert client.get("/api/homologation/research/runs",
                      headers=HEADERS).json()["total"] == before


def test_historical_comparison_is_authenticated_descriptive_and_read_only(isolated_db):
    client = TestClient(core.app)
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="research-compare"' in page.text
    assert 'id="compare-research"' in page.text
    assert "renderCompareOptions(studies.runs)" in page.text
    assert "comparisonRequest" in page.text

    uploaded = client.post("/api/homologation/assets", json=sample(),
                           headers=HEADERS)
    assert uploaded.status_code == 200, uploaded.text
    dataset_id = uploaded.json()["id"]
    research_path = "/api/homologation/research/runs/uploaded/" + dataset_id
    first = client.post(research_path, headers=HEADERS)
    second = client.post(research_path, headers=HEADERS)
    assert first.status_code == second.status_code == 200
    left, right = first.json()["id"], second.json()["id"]
    path = f"/api/homologation/research/compare?left={left}&right={right}"

    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-NEXORA-STAGE4-KEY": "wrong"}).status_code == 401
    assert client.get(f"/api/homologation/research/compare?left={left}&right={left}",
                      headers=HEADERS).status_code == 422
    assert client.get(f"/api/homologation/research/compare?left=invalid&right={right}",
                      headers=HEADERS).status_code == 422
    assert client.get(f"/api/homologation/research/compare?left={'0' * 32}&right={right}",
                      headers=HEADERS).status_code == 404

    total_before = client.get("/api/homologation/research/runs",
                              headers=HEADERS).json()["total"]
    comparison = client.get(path, headers=HEADERS)
    assert comparison.status_code == 200, comparison.text
    data = comparison.json()
    assert data["format_version"] == "nexora-descriptive-comparison-v1"
    assert data["descriptive_only"] is True
    assert data["provenance_verified"] is False
    assert data["financial_execution_allowed"] is False
    assert data["performance_ranking"] is None
    assert data["same_source_and_dataset"] is True
    assert data["shared_timeframes_seconds"] == [300]
    assert data["left"]["id"] == left
    assert data["right"]["id"] == right
    assert data["left"]["result_sha256"] == first.json()["result_sha256"]
    assert data["right"]["result_sha256"] == second.json()["result_sha256"]
    assert data["left"]["dataset_identity"] == data["right"]["dataset_identity"]
    assert data["left"]["timeframes"][0]["observations"] == 96
    assert data["left"]["provenance_verified"] is False
    assert data["right"]["provenance_verified"] is False
    assert KEY not in comparison.text
    assert client.get(path, headers=HEADERS).json() == data
    assert client.get("/api/homologation/research/runs",
                      headers=HEADERS).json()["total"] == total_before
    assert not any("/buy" in route.path or "/orders" in route.path
                   for route in core.app.routes)


def test_historical_comparison_never_ranks_mixed_provenance(isolated_db):
    client = TestClient(core.app)
    upload = sample("DECLARED_REAL_UNVERIFIED")
    created = client.post("/api/homologation/assets", json=upload,
                          headers=HEADERS)
    assert created.status_code == 200
    uploaded = client.post(
        "/api/homologation/research/runs/uploaded/" + created.json()["id"],
        headers=HEADERS,
    )
    assert uploaded.status_code == 200
    synthetic = client.post("/api/homologation/research/runs",
                            headers=HEADERS)
    assert synthetic.status_code == 200, synthetic.text
    path = (
        "/api/homologation/research/compare?left="
        + uploaded.json()["id"] + "&right=" + synthetic.json()["id"]
    )
    compared = client.get(path, headers=HEADERS)
    assert compared.status_code == 200, compared.text
    result = compared.json()
    assert result["same_source_and_dataset"] is False
    assert result["performance_ranking"] is None
    assert "not equivalent" in result["context_note"]
    assert result["left"]["synthetic"] is False
    assert result["right"]["synthetic"] is True
    assert result["left"]["origin_type"] == "DECLARED_REAL_UNVERIFIED"
    assert result["left"]["provenance_verified"] is False
    assert result["financial_execution_allowed"] is False


def test_research_archive_pagination_filters_auth_and_non_mutation(isolated_db):
    client = TestClient(core.app)
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="research-search"' in page.text
    assert 'id="research-state"' in page.text
    assert 'id="research-prev"' in page.text
    assert 'id="research-next"' in page.text
    assert "id_prefix" in page.text

    uploaded = client.post("/api/homologation/assets", json=sample(),
                           headers=HEADERS)
    assert uploaded.status_code == 200, uploaded.text
    dataset = uploaded.json()["id"]
    ids = []
    for _ in range(4):
        response = client.post(
            "/api/homologation/research/runs/uploaded/" + dataset,
            headers=HEADERS,
        )
        assert response.status_code == 200, response.text
        ids.append(response.json()["id"])

    base = "/api/homologation/research/runs"
    assert client.get(base + "?limit=2").status_code == 401
    assert client.get(base + "?limit=2&offset=0", headers=HEADERS).status_code == 200
    first = client.get(base + "?limit=2&offset=0", headers=HEADERS).json()
    second = client.get(base + "?limit=2&offset=2", headers=HEADERS).json()
    last = client.get(base + "?limit=2&offset=4", headers=HEADERS).json()

    assert first["total"] == second["total"] == last["total"] == 4
    assert first["offset"] == 0 and second["offset"] == 2
    assert first["limit"] == 2
    assert first["has_more"] is True
    assert second["has_more"] is False
    assert last["has_more"] is False and last["runs"] == []
    assert {r["id"] for r in first["runs"]}.isdisjoint(
        {r["id"] for r in second["runs"]})
    all_ids = [r["id"] for r in first["runs"] + second["runs"]]
    assert set(all_ids) == set(ids)
    assert all(r["state"] == "COMPLETE" for r in first["runs"] + second["runs"])
    assert client.get(base, headers=HEADERS).json()["total"] == 4

    filtered = client.get(
        base + "?limit=2&state=COMPLETE&id_prefix=" + ids[0][:12].upper(),
        headers=HEADERS,
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 1
    assert filtered.json()["runs"][0]["id"] == ids[0]
    assert filtered.json()["has_more"] is False

    no_errors = client.get(base + "?state=ERROR", headers=HEADERS)
    assert no_errors.status_code == 200
    assert no_errors.json()["total"] == 0 and no_errors.json()["runs"] == []
    for query in ("offset=-1", "offset=100001", "limit=101",
                  "state=RUNNING", "id_prefix=not-hex"):
        invalid = client.get(base + "?" + query, headers=HEADERS)
        assert invalid.status_code == 422, (query, invalid.text)

    restored = client.get(
        "/api/homologation/research/runs/" + all_ids[-1], headers=HEADERS)
    assert restored.status_code == 200
    assert len(restored.json()["stages"]) == 7
    assert client.get(base, headers=HEADERS).json()["total"] == 4
    assert not any("/buy" in route.path or "/orders" in route.path
                   for route in core.app.routes)

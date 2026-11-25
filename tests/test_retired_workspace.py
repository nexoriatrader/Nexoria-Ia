"""Retired workspace must not be exposed; shared data remains untouched."""
from pathlib import Path
from app import main

ROOT = Path(__file__).resolve().parents[1]


def test_retired_workspace_removed_from_navigation_and_routes():
    html = (ROOT / "app" / "index.html").read_text(encoding="utf-8")
    assert 'href="/nexora2"' not in html
    assert not (ROOT / "app" / "nexora2.html").exists()
    assert not any(
        getattr(route, "path", "").startswith(("/nexora2", "/api/nexora2/"))
        for route in main.app.routes
    )


def test_retired_research_routes_and_modules_absent():
    routes = {getattr(route, "path", "") for route in main.app.routes}
    assert "/api/research/asset-scanner" not in routes
    assert "/api/research/strategy-lab" not in routes
    assert not (ROOT / "app" / "intelligence2").exists()
    assert not (ROOT / "app" / "asset_scanner.py").exists()

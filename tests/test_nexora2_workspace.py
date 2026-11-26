"""Retired educational pages stay unavailable while operational setup remains."""
from pathlib import Path
from fastapi.testclient import TestClient
from app import main


def test_removed_standalone_pages_return_not_found():
    client=TestClient(main.app)
    assert client.get('/configuracao-educativa').status_code==404
    assert client.get('/nexora2').status_code==404


def test_educational_menu_and_views_are_removed_without_hiding_robot_configuration():
    html=(Path(__file__).resolve().parents[1]/'app'/'index.html').read_text(encoding='utf-8')
    for retired in ('href="/configuracao-educativa"','href="/nexora2"',
                    'data-view="features"','data-view="strategies"',
                    '<section id="features"','<section id="strategies"',
                    'function setEducationTab'):
        assert retired not in html
    assert "if(!APP_PAGES.has(v))v='dashboard'" in html
    assert '<section id="robot-config"' in html
    assert 'name="autoStrategy"' in html
    assert 'Nexora IA Estratégias' in html

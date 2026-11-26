from pathlib import Path


INDEX_HTML = Path(__file__).resolve().parents[1] / "app" / "index.html"


def test_mobile_dashboard_keeps_the_primary_navigation_sticky_and_visible():
    html = INDEX_HTML.read_text()

    assert '<nav id="primaryNavigation" class="primary-nav hidden" aria-label="Navegação principal">' in html
    assert ".primary-nav{position:sticky;top:0" in html
    assert "body:has(#dashboard:not(.hidden)) .primary-nav{display:none}" not in html
    assert "const publicPage=v==='welcome'||v==='auth-guide';" in html
    assert "$('primaryNavigation').classList.toggle('hidden',publicPage||v==='setup')" in html
    assert "$('topAccessSummary').classList.toggle('hidden',publicPage)" in html
    assert ".nexora-logo-only{align-items:center;justify-content:space-between" in html
    primary_nav = html.split('<nav id="primaryNavigation"', 1)[1].split("</nav>", 1)[0]
    for view in ("app-home", "account", "dashboard", "manual-dashboard", "results", "cataloger"):
        assert f'data-view="{view}"' in primary_nav
    assert 'class="nav-link nav-broker-link"' in primary_nav
    assert 'Nexora Manual' in primary_nav
    assert 'Nexora Estratégias' in primary_nav
    assert 'data-view="robot-config"' not in primary_nav


def test_old_fixed_bottom_dashboard_navigation_is_removed():
    html = INDEX_HTML.read_text()

    assert "nx-neo-mobile-nav" not in html
    assert 'aria-label="Navegação rápida do painel"' not in html
    assert "padding-bottom:88px" not in html

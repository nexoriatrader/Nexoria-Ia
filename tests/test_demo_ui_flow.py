from pathlib import Path


def load_ui():
    return (Path(__file__).resolve().parents[1]/"app"/"index.html").read_text(encoding="utf-8")


def test_demo_interface_has_confirmed_manual_test_and_official_balance():
    html=load_ui()
    assert "TESTAR UMA ENTRADA DEMO" in html
    assert "window.confirm(" in html
    assert "confirm:'DEMO_ONLY'" in html
    assert "request_key:key" in html
    assert "/api/demo/buy" in html
    assert "/api/demo/balance" in html
    assert "Saldo Deriv Demo" in html


def test_official_contract_history_replaces_local_simulator():
    html=load_ui()
    assert "Histórico oficial" in html
    assert "/api/demo/orders" in html
    assert "state==='SETTLED'" in html
    for forbidden in ("LIGAR SIMULAÇÃO", "Histórico de simulação", "Resultado virtual",
                      "/api/start", "/api/stop", "/api/history"):
        assert forbidden not in html


def test_mobile_navigation_and_single_primary_robot_control():
    html=load_ui()
    assert 'id="drawer"' in html
    assert 'id="menuButton"' in html
    assert 'id="robotButton"' in html
    assert 'disabled aria-disabled="true"' in html
    assert "AutoPilot" in html
    assert "loadDemoOrders()" in html
    assert "/api/config/current" in html
    assert "data-view=\"results\"" in html


def test_continuity_prompt_clarifies_demo_not_real():
    root=Path(__file__).resolve().parents[1]
    text=(root/"PROMPT_MANUS_DEMO_SEM_BLOQUEIOS_DESNECESSARIOS.md").read_text(encoding="utf-8")
    assert "NEXORA_DEMO_BUY_ENABLED=true" in text
    assert "Não executar compras reais" in text


def test_real_mode_is_explicitly_unavailable_for_purchases():
    html=load_ui()
    assert "Indisponível para compras nesta fase" in html
    assert "A aplicação não envia ordens em conta real" in html

def test_inline_javascript_syntax_is_valid(tmp_path):
    """Catch malformed inline JavaScript before it blocks OAuth login initialization."""
    import re
    import shutil
    import subprocess
    import pytest

    html = load_ui()
    blocks = re.findall(r"<script(?:\\s[^>]*)?>(.*?)</script>", html, flags=re.S | re.I)
    assert blocks, "The OAuth login UI must have a working initialization script"
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for syntax checking")
    for index, block in enumerate(blocks):
        path = tmp_path / f"inline_{index}.js"
        path.write_text(block, encoding="utf-8")
        result = subprocess.run([node, "--check", str(path)],
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr

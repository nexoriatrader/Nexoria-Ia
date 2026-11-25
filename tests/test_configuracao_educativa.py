"""The educational preview is isolated from account and broker operations."""
from pathlib import Path
import re
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "configuracao_educativa.html").read_text(encoding="utf-8")


def test_three_step_preview_and_no_execution_controls():
    assert re.findall(r'id="step([123])"', HTML) == ["1", "2", "3"]
    assert re.findall(r'name="study" value="([^"]+)"', HTML) == ["pulso", "trend", "rsi"]
    assert "connect-src 'none'" in HTML
    assert "form-action 'none'" in HTML
    assert "Operações reais</dt><dd>Desativadas" in HTML
    for forbidden in ("/api/", "fetch(", "XMLHttpRequest", "WebSocket(", "localStorage", "sessionStorage"):
        assert forbidden not in HTML


def test_preview_script_syntax(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    script = re.search(r"<script>(.*?)</script>", HTML, re.S).group(1)
    path = tmp_path / "preview.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

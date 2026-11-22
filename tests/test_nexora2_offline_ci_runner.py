"""Safety regressions for local NEXORA CI without GitHub Actions quota."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

RUNNER_FILE = Path(__file__).resolve().parents[1] / "scripts" / "run_nexora2_offline_ci.py"


def runner():
    spec = importlib.util.spec_from_file_location("nexora2_local_ci", RUNNER_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_test_env_replaces_production_configuration(tmp_path):
    run = runner()
    inherited = {
        "PATH": "/usr/bin",
        "DATABASE_URL": "production_database_should_never_be_inherited",
        "DERIV_TOKEN": "production_token_should_never_be_inherited",
        "PYTHONPATH": "/untrusted/host/modules",
        "NEXORA_FERNET_KEY": "production_key_should_never_be_inherited",
        "NEXORA_REAL_RELEASE_APPROVED": "true",
        "NEXORA_REAL_AUTOPILOT_ENABLED": "true",
        "NEXORA_DEMO_BUY_ENABLED": "true",
    }
    env = run.isolated_env(tmp_path / "new_research.sqlite", inherited)
    assert env["DATABASE_URL"] == f'sqlite:///{(tmp_path / "new_research.sqlite").as_posix()}'
    assert env["NEXORA_PUBLIC_URL"] == "http://localhost:8000"
    assert env["NEXORA_REAL_AUTOPILOT_ENABLED"] == "false"
    assert env["NEXORA_REAL_RELEASE_APPROVED"] == "false"
    assert env["NEXORA_DEMO_BUY_ENABLED"] == "false"
    assert env["NEXORA_INTELLIGENCE_READONLY_ENABLED"] == "false"
    assert env["NEXORA_FERNET_KEY"] != inherited["NEXORA_FERNET_KEY"]
    assert "DERIV_TOKEN" not in env
    assert "PYTHONPATH" not in env
    assert "production" not in str(env)
    assert env["PATH"] == "/usr/bin"


def test_local_plan_covers_ci_layers_without_deployment():
    run = runner()
    full = run.command_plan("full", "python3")
    focused = run.command_plan("focused", "python3")
    assert len(full) == 5
    assert len(focused) == 4
    assert full[0][1] == ["python3", "-m", "compileall", "-q", "app", "tests"]
    assert any("scripts/check_repository_security.py" in cmd for _, cmd in full)
    assert any("scripts/research_release_diff_guard.py" in cmd for _, cmd in full)
    assert full[-1][1] == ["python3", "-m", "pytest", "-q"]
    assert "tests/test_nexora2_integrated_workspace.py" in focused[-1][1]
    assert "tests/test_nexora2_uploaded_ohlc.py" in focused[-1][1]
    for _, command in full + focused:
        assert not any("deploy" in arg.lower() for arg in command)


def test_local_runner_exits_on_failure_and_writes_only_nonsecret_report(
    tmp_path, monkeypatch
):
    run = runner()
    monkeypatch.setenv("NEXORA_REAL_RELEASE_APPROVED", "true")
    monkeypatch.setenv("DATABASE_URL", "production_database_should_not_leak")
    monkeypatch.setenv("DERIV_TOKEN", "production_token_should_not_leak")
    observed = []

    def fake_run(command, *, cwd, env, check):
        assert check is False
        assert cwd == RUNNER_FILE.parents[1]
        assert env["DATABASE_URL"].startswith("sqlite:///")
        assert "production" not in str(env)
        assert "DERIV_TOKEN" not in env
        assert env["NEXORA_REAL_RELEASE_APPROVED"] == "false"
        observed.append(command)
        return SimpleNamespace(returncode=3 if len(observed) == 2 else 0)

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    report_path = tmp_path / "result.json"
    assert run.main(["--mode", "full", "--report", str(report_path)]) == 1
    report = json.loads(report_path.read_text())
    assert len(report["steps"]) == 2
    assert report["passed"] is False
    assert report["financial_execution_disabled"] is True
    assert report["isolated_database"] is True
    assert "production" not in report_path.read_text()
    assert "NEXORA_FERNET_KEY" not in report_path.read_text()
    assert len(observed) == 2

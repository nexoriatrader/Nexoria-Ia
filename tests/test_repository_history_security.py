"""Read-only historical credential scanner regression tests with disposable Git repos."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def git(folder: Path, *args: str):
    return subprocess.run(
        ["git", "-C", str(folder), *args], check=True, capture_output=True, text=True,
        timeout=12,
    ).stdout.strip()


def new_repo(tmp_path: Path):
    root = tmp_path / "historical-fixture"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "NEXORA Test")
    (root / "README.md").write_text("Public research example, no secrets.\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "safe initial commit")
    return root


def load_scanner(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import check_repository_history
    return check_repository_history


def test_history_scan_passes_clean_reachable_commits(tmp_path, monkeypatch):
    root = new_repo(tmp_path)
    scanner = load_scanner(monkeypatch)
    result = scanner.audit_history(root)
    assert result["findings"] == []
    assert result["git_objects_examined"] > 0
    assert result["historical_text_blobs_scanned"] > 0
    assert result["refs_inspected"] > 0


def test_deleted_historical_credentials_are_detected_without_leaking_values(
        tmp_path, monkeypatch):
    root = new_repo(tmp_path)
    # This value is assembled only in the test fixture, not stored in repo.
    mock_token = "gh" + "p_" + "a" * 36
    (root / "formerly-private.txt").write_text("token=" + mock_token + "\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "intermediate with mock secret")
    (root / "formerly-private.txt").unlink()
    git(root, "add", "-A")
    git(root, "commit", "-qm", "delete old token")
    scanner = load_scanner(monkeypatch)
    results = scanner.audit_history(root)
    assert any(
        name == "formerly-private.txt" and kind == "github_personal_access_token"
        for name, line, kind in results["findings"]
    )
    assert mock_token not in str(results)


def test_historical_env_filename_detected_after_deletion(tmp_path, monkeypatch):
    root = new_repo(tmp_path)
    (root / ".env").write_text("PLACEHOLDER=not-a-secret\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "historical sensitive filename")
    (root / ".env").unlink()
    git(root, "add", "-A")
    git(root, "commit", "-qm", "delete env file")
    scanner = load_scanner(monkeypatch)
    results = scanner.audit_history(root)
    assert any(
        name == ".env" and kind == "sensitive_historical_filename"
        for name, line, kind in results["findings"]
    )


def test_history_scan_rejects_shallow_clone(tmp_path, monkeypatch):
    root = new_repo(tmp_path)
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--quiet", "--depth=1",
         root.resolve().as_uri(), str(shallow)],
        check=True, capture_output=True, timeout=12,
    )
    scanner = load_scanner(monkeypatch)
    with pytest.raises(RuntimeError, match="raso"):
        scanner.audit_history(shallow)

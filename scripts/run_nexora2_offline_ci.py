"""Run NEXORA research regression tests locally without GitHub Actions.

Only disposable SQLite and a new test encryption key are used. No deploy,
production migrations, broker orders, or financial execution.

Usage:
    python scripts/run_nexora2_offline_ci.py --mode focused
    python scripts/run_nexora2_offline_ci.py --mode full
    python scripts/run_nexora2_offline_ci.py --mode full --report /tmp/nexora-check.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FOCUSED_TESTS = (
    "tests/test_nexora2_workspace.py",
    "tests/test_nexora2_persisted_library.py",
    "tests/test_nexora2_personal_history.py",
    "tests/test_nexora2_dashboard_clarity.py",
    "tests/test_nexora2_uploaded_ohlc.py",
    "tests/test_nexora2_ohlc_compare.py",
    "tests/test_nexora2_integrated_workspace.py",
    "tests/test_intelligence2_context_timeframe.py",
)

# Never inherit DATABASE_URL, Deriv tokens, deployment keys or financial flags.
INHERITED_ENV = frozenset({
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "LANG", "LC_ALL", "TMP", "TEMP", "TMPDIR", "VIRTUAL_ENV",
    "PYTHONPATH", "PYTHONIOENCODING", "PYTHONUTF8", "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE", "PIP_CERT",
})


def isolated_env(database_path: Path, inherited: dict[str, str] | None = None) -> dict[str, str]:
    from cryptography.fernet import Fernet

    incoming = os.environ if inherited is None else inherited
    env = {name: value for name, value in incoming.items() if name in INHERITED_ENV}
    env.update({
        "DATABASE_URL": f"sqlite:///{database_path.as_posix()}",
        "NEXORA_FERNET_KEY": Fernet.generate_key().decode("ascii"),
        "NEXORA_PUBLIC_URL": "http://localhost:8000",
        "NEXORA_DEMO_BUY_ENABLED": "false",
        "NEXORA_INTELLIGENCE_READONLY_ENABLED": "false",
        "NEXORA_REAL_AUTOPILOT_ENABLED": "false",
        "NEXORA_REAL_RELEASE_APPROVED": "false",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


def command_plan(mode: str, python: str) -> list[tuple[str, list[str]]]:
    if mode not in ("focused", "full"):
        raise ValueError("Unknown test mode")
    plan = [
        ("Python syntax", [python, "-m", "compileall", "-q", "app", "tests"]),
        ("Research release guard", [python, "scripts/research_release_diff_guard.py"]),
        ("Repository secret scan", [python, "scripts/check_repository_security.py"]),
    ]
    if mode == "focused":
        plan.append(("NEXORA research regressions", [python, "-m", "pytest", "-q", *FOCUSED_TESTS]))
    else:
        plan.extend([
            ("Research release guard regression", [python, "-m", "pytest", "-q", "tests/test_ci_research_release_diff_guard.py"]),
            ("Complete Python test suite", [python, "-m", "pytest", "-q"]),
        ])
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("focused", "full"), default="focused")
    parser.add_argument("--report", type=Path, help="Optional JSON result report path")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    required = (root / "app" / "main.py", root / "requirements.txt", root / "tests")
    if not all(path.exists() for path in required):
        print("Error: run this from an existing NEXORA repository checkout.", file=sys.stderr)
        return 2
    plan = command_plan(args.mode, sys.executable)
    if args.dry_run:
        for label, command in plan:
            print(f"[DRY RUN] {label}: {' '.join(command)}")
        return 0

    try:
        import cryptography  # noqa: F401; ephemeral encryption key
        import pytest  # noqa: F401; test runner
    except ImportError:
        print("Install test dependencies: python -m pip install -r requirements.txt", file=sys.stderr)
        return 2

    results: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="nexora2-offline-ci-") as directory:
        env = isolated_env(Path(directory) / "nexora_test.sqlite")
        print(f"NEXORA offline CI ({args.mode}): isolated SQLite, ephemeral key; financial flags OFF")
        for label, command in plan:
            print(f"\n=== {label} ===", flush=True)
            begin = time.monotonic()
            try:
                completed = subprocess.run(command, cwd=root, env=env, check=False)
                exit_code = completed.returncode
            except OSError as exc:
                print(f"Could not start {label}: {type(exc).__name__}", file=sys.stderr)
                exit_code = 127
            elapsed = round(time.monotonic() - begin, 2)
            results.append({"name": label, "exit_code": exit_code, "seconds": elapsed})
            print(f"=== {label}: {'PASS' if exit_code == 0 else 'FAIL'} ({elapsed}s) ===", flush=True)
            if exit_code != 0:
                break

    success = len(results) == len(plan) and all(item["exit_code"] == 0 for item in results)
    report = {
        "mode": args.mode,
        "passed": success,
        "isolated_database": True,
        "financial_execution_disabled": True,
        "steps": results,
        "note": "Local checks, not GitHub Actions or disposable PostgreSQL integration tests.",
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Local report written: {args.report}")
    print(f"\nRESULT: {'PASS' if success else 'FAIL'} ({len(results)}/{len(plan)} steps completed)")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())

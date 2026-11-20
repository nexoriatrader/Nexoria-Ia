"""Guard the isolated I01-I03 release diff before and after its merge.

Before merge: compare the release PR with origin/main at its common ancestor.
After merge: compare the merge commit with its *first parent*. Comparing
origin/main...HEAD on main is always empty and incorrectly blocks CI.

Unrelated future merges to main, and unrelated PRs, are intentionally ignored:
this is a release-specific guard, not a repository-wide file allowlist.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

RELEASE_BRANCH = "release/intelligence20-i01-i03-readonly-main-isolated"
RELEASE_MARKER = "docs/INTELLIGENCE_20_I01_I03_MAIN_ISOLATED.md"
ALLOWED_FILES = frozenset({
    ".github/workflows/test.yml",
    ".github/workflows/intelligence20-pg17-compat.yml",
    "app/asset_scanner.py",
    "app/automation_markets.py",
    "app/main.py",
    "app/intelligence2/__init__.py",
    "app/intelligence2/market_data.py",
    "app/intelligence2/market_scanner.py",
    "app/intelligence2/context_engine.py",
    "app/intelligence2/timeframe_engine.py",
    "tests/test_intelligence2_market_data.py",
    "tests/test_intelligence2_market_scanner.py",
    "tests/test_intelligence2_context_timeframe.py",
    "tests/test_intelligence2_main_isolated_release.py",
    "tests/test_postgres17_main_research_release.py",
    RELEASE_MARKER,
})


def git_lines(*args: str, cwd: str | Path | None = None) -> list[str]:
    return subprocess.check_output(
        ["git", *args], cwd=cwd, text=True,
    ).splitlines()


def release_changes(
    github_ref: str,
    github_head_ref: str = "",
    *,
    head: str = "HEAD",
    base: str = "origin/main",
    cwd: str | Path | None = None,
) -> tuple[set[str] | None, str]:
    """Return relevant paths, or None if the release guard is inapplicable."""
    if github_ref == "refs/heads/main":
        parents = git_lines("rev-list", "--parents", "-n", "1", head, cwd=cwd)
        if not parents:
            raise RuntimeError("Cannot inspect main commit ancestry")
        ancestry = parents[0].split()
        if len(ancestry) != 3:
            return None, "main: commit is not a two-parent merge"
        changed = set(git_lines("diff", "--name-only", ancestry[1], head, cwd=cwd))
        if RELEASE_MARKER not in changed:
            return None, "main: merge is unrelated to the isolated release"
        return changed, "main: compare merge against first parent"
    if (
        github_head_ref == RELEASE_BRANCH
        or github_ref == "refs/heads/" + RELEASE_BRANCH
    ):
        return set(git_lines("diff", "--name-only", f"{base}...{head}", cwd=cwd)), (
            "release PR: compare against main merge base"
        )
    return None, "unrelated branch or pull request"


def validate_release_changes(changed: set[str] | None) -> None:
    if changed is None:
        return
    if not changed:
        raise AssertionError("Isolated release diff is unexpectedly empty")
    forbidden = changed - ALLOWED_FILES
    if forbidden:
        raise AssertionError(
            "Release modifies forbidden P0/F01, broker or schema paths: "
            + str(sorted(forbidden))
        )


def main() -> None:
    changed, reason = release_changes(
        os.environ.get("GITHUB_REF", ""),
        os.environ.get("GITHUB_HEAD_REF", ""),
    )
    validate_release_changes(changed)
    if changed is None:
        print("Research release diff guard not applicable:", reason)
    else:
        print("Research release diff allowlist validated:", reason, len(changed), "files")


if __name__ == "__main__":
    main()

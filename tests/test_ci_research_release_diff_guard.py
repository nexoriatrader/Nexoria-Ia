"""Regression for I01-I03 CI diff guard, including the real merged SHA."""
import pytest

from scripts import research_release_diff_guard as guard

BASE = "d51aedba27458057e2803346da85f887fd7aede9"
PR_HEAD = "cfbbf34fe8cd41771fcfcd5b78922d580b966154"
MERGED_HEAD = "c188f1a1d1c65a1ebf57c39a3e695ab550afa5cc"


def test_release_pr_compares_against_premerge_main():
    changed, method = guard.release_changes(
        "refs/pull/57/merge", guard.RELEASE_BRANCH,
        head=PR_HEAD, base=BASE,
    )
    assert method.startswith("release PR:")
    assert guard.RELEASE_MARKER in changed
    assert len(changed) == 16
    guard.validate_release_changes(changed)


def test_same_release_remains_checked_after_merge_into_main():
    before, _ = guard.release_changes(
        "refs/pull/57/merge", guard.RELEASE_BRANCH,
        head=PR_HEAD, base=BASE,
    )
    after, method = guard.release_changes(
        "refs/heads/main", head=MERGED_HEAD,
    )
    assert method.startswith("main:")
    assert before == after
    guard.validate_release_changes(after)


def test_unrelated_main_push_and_pr_do_not_fail_release_guard(monkeypatch):
    def fake_git(*args, cwd=None):
        if args[0] == "rev-list":
            return ["fake_merge first_parent second_parent"]
        if args[0] == "diff":
            return ["app/other_feature.py"]
        raise AssertionError("unexpected git command")
    monkeypatch.setattr(guard, "git_lines", fake_git)
    changes, _ = guard.release_changes("refs/heads/main")
    assert changes is None
    guard.validate_release_changes(changes)
    changes, _ = guard.release_changes(
        "refs/pull/58/merge", "fix/ci-research-release-diff-after-merge"
    )
    assert changes is None
    guard.validate_release_changes(changes)


def test_nonmerge_main_push_ignored(monkeypatch):
    monkeypatch.setattr(guard, "git_lines", lambda *a, **kw: ["single_sha parent_sha"])
    changed, _ = guard.release_changes("refs/heads/main")
    assert changed is None


@pytest.mark.parametrize("unsafe_path", [
    "app/db.py",
    "app/real_runtime.py",
    "migrations/F01_GLOBAL_FENCING_V1_POSTGRES.sql",
])
def test_financial_or_schema_diff_remains_blocked(unsafe_path):
    with pytest.raises(AssertionError, match="forbidden"):
        guard.validate_release_changes(
            {guard.RELEASE_MARKER, unsafe_path}
        )


def test_empty_relevant_release_is_rejected():
    with pytest.raises(AssertionError, match="empty"):
        guard.validate_release_changes(set())

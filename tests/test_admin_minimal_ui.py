"""Keep the owner-only admin focused on user access management.

These assertions protect the lean dashboard without removing server-side
access revocation, audit evidence, or protected research infrastructure.
"""
from pathlib import Path

ADMIN_HTML = Path(__file__).resolve().parents[1] / "app" / "admin.html"


def test_admin_keeps_user_access_workflow():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    for token in (
        'id="loginForm"',
        'id="userSearch"',
        'id="userId"',
        'id="days"',
        'value="add_hours"',
        'value="remove_hours"',
        'value="suspend"',
        'value="resume"',
        'value="refund_revoke"',
        "/api/admin/access/users",
        "/api/admin/access/adjust",
    ):
        assert token in html


def test_admin_does_not_load_legacy_research_or_audit_ui():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    for token in (
        'id="strategyLab"',
        'id="researchResults"',
        'id="audit"',
        "/api/admin/access/audit",
        "/api/admin/research/",
        "loadResearch()",
        "loadM1Coverage()",
        "loadMultiResearch()",
        "loadPullback()",
        "Promise.all([refresh()",
    ):
        assert token not in html
    assert "if(s.authenticated){await refresh();}" in html

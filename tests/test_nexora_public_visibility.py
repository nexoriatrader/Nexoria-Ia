"""No external network calls: verify public-repository status-check semantics."""
from __future__ import annotations

import io
import json
import urllib.error

from scripts.check_nexora_public_visibility import REPO, check_visibility


class FakeResponse:
    def __init__(self, payload):
        self.content = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self.content

    def __exit__(self, exc_type, exc, tb):
        self.content.close()


def test_exact_public_repo_is_confirmed_only_when_anonymously_visible():
    def opener(request, *, timeout):
        assert request.full_url.endswith("/repos/" + REPO)
        assert "Authorization" not in request.headers
        return FakeResponse({
            "full_name": REPO,
            "private": False,
            "visibility": "public",
        })
    state, detail = check_visibility(opener)
    assert state == "PUBLIC"


def test_private_repo_cannot_be_reported_public():
    for payload in (
        {"full_name": REPO, "private": True, "visibility": "private"},
        {"full_name": REPO, "private": True, "visibility": "public"},
        {"full_name": REPO, "private": False, "visibility": "private"},
    ):
        state, detail = check_visibility(
            lambda request, *, timeout: FakeResponse(payload)
        )
        assert state != "PUBLIC"


def test_wrong_repo_or_unexpected_json_never_marks_public():
    for payload in (
        {"full_name": "another-owner/another-repo", "private": False,
         "visibility": "public"},
        [],
        {"full_name": None, "private": False, "visibility": "public"},
    ):
        state, detail = check_visibility(
            lambda request, *, timeout: FakeResponse(payload)
        )
        assert state == "UNKNOWN"


def test_private_404_and_api_errors_are_distinguished():
    def error(code):
        def opener(request, *, timeout):
            raise urllib.error.HTTPError(request.full_url, code, "failure", {}, None)
        return opener

    assert check_visibility(error(404))[0] == "NOT_PUBLIC"
    assert check_visibility(error(503))[0] == "UNKNOWN"

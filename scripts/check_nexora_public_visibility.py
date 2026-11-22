"""Check that the exact NEXORA repo is publicly visible via GitHub's public API.

This uses an unauthenticated read-only request. It never changes repository
settings, enables financial execution, deploys, merges or prints credentials.

Usage:
    python scripts/check_nexora_public_visibility.py

Exit 0 only if the exact repository is anonymously visible and GitHub
explicitly identifies it as public. Exit 1 if still private/not found, and 2
for temporary API/network errors or an unexpected response.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

REPO = "nexoriatrader/Nexoria-Ia"
URL = f"https://api.github.com/repos/{REPO}"


def check_visibility(opener=urllib.request.urlopen) -> tuple[str, str]:
    request = urllib.request.Request(
        URL, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "NEXORA-public-visibility-readonly-check",
        },
    )
    try:
        with opener(request, timeout=12) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "NOT_PUBLIC", "GitHub public API returned 404"
        return "UNKNOWN", f"GitHub API HTTP {exc.code}"
    except (OSError, ValueError, json.JSONDecodeError):
        return "UNKNOWN", "GitHub public API unavailable or invalid response"

    if (not isinstance(data, dict)
            or data.get("full_name", "").lower() != REPO.lower()):
        return "UNKNOWN", "Repository identity could not be verified"
    if data.get("private") is False and data.get("visibility") == "public":
        return "PUBLIC", "Correct NEXORA repository anonymously accessible"
    if data.get("private") is True or data.get("visibility") == "private":
        return "NOT_PUBLIC", "Repository still marked private"
    return "UNKNOWN", "Repository visibility not explicitly public"


def main() -> int:
    state, reason = check_visibility()
    print(f"NEXORA public repository verification: {state}. {reason}.")
    if state == "PUBLIC":
        print("This does not validate Actions quota, CI tests or code safety.")
        return 0
    return 1 if state == "NOT_PUBLIC" else 2


if __name__ == "__main__":
    sys.exit(main())

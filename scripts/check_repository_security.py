"""Conservative tracked-file secret audit for the NEXORA GitHub repository.

This detects obvious accidental credential commits. It complements, but does
not replace, GitHub secret scanning or a full scan of repository history.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


# Build regular expressions rather than embedding any sample credentials.
SIGNATURES = {
    "github_personal_access_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{24,}\b"),
    "github_fine_grained_token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    "stripe_api_secret": re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{18,}\b"),
    "supabase_secret_key": re.compile(r"\bsb_secret_[A-Za-z0-9_-]{20,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private_key_pem": re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    "postgres_password_url": re.compile(
        r"postgres(?:ql)?://[^\s/:@]+:[^@\s/:]+@",
        re.IGNORECASE,
    ),
    "fernet_literal": re.compile(
        r"\bNEXORA_FERNET_KEY\s*=\s*['\\"]?[A-Za-z0-9_-]{43}="
    ),
}

FORBIDDEN_BASENAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "id_rsa", "id_ed25519", "credentials.json", "service-account.json",
}
FORBIDDEN_EXTENSIONS = {".pem", ".p12", ".pfx", ".key", ".sqlite", ".db"}
MAX_TEXT_BYTES = 4_000_000


def suspicious_filename(path: Path) -> bool:
    return path.name.lower() in FORBIDDEN_BASENAMES or path.suffix.lower() in FORBIDDEN_EXTENSIONS


def scan_text(content: str) -> list[tuple[int, str]]:
    issues = []
    for number, line in enumerate(content.splitlines(), 1):
        for name, pattern in SIGNATURES.items():
            if pattern.search(line):
                issues.append((number, name))
    return issues


def audit_repository(root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached"], cwd=root,
        check=True, stdout=subprocess.PIPE,
    )
    findings = []
    for raw in completed.stdout.split(b"\x00"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8", errors="replace"))
        location = root / relative
        if suspicious_filename(relative):
            findings.append(f"{relative}: sensitive filename is tracked")
        if not location.is_file() or location.stat().st_size > MAX_TEXT_BYTES:
            continue
        data = location.read_bytes()
        if b"\x00" in data[:1024]:
            continue
        content = data.decode("utf-8", errors="replace")
        for number, kind in scan_text(content):
            findings.append(f"{relative}:{number}: {kind}")
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    findings = audit_repository(root)
    if findings:
        print(f"Security audit failed: {len(findings)} potential leak(s) detected.")
        print("\n".join(findings))
        print("Do not paste credentials into issue comments or CI logs.")
        print("Investigate, remove the exposure and rotate any compromised secret.")
        return 1
    print("Security audit passed: no high-confidence leaks detected in tracked HEAD files.")
    print("This does not certify Git history, dependency integrity, or runtime secrets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

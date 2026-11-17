"""Regression for conservative, offline credential scanning."""
from pathlib import Path
from scripts.check_repository_security import scan_text, suspicious_filename


def test_sensitive_filenames_are_not_allowed_to_enter_repository():
    assert suspicious_filename(Path(".env"))
    assert suspicious_filename(Path(".env.production"))
    assert suspicious_filename(Path("my-private.key"))
    assert suspicious_filename(Path("session.sqlite"))
    assert not suspicious_filename(Path(".env.example"))
    assert not suspicious_filename(Path("requirements.txt"))


def test_known_secret_shapes_detected_without_printing_values():
    token = "gh" + "p_" + "a" * 36
    stripe = "sk" + "_live_" + "b" * 30
    assert [category for _, category in scan_text("cred=" + token)] == [
        "github_personal_access_token"
    ]
    assert [category for _, category in scan_text("key=" + stripe)] == [
        "stripe_api_secret"
    ]


def test_safe_examples_not_flagged():
    assert not scan_text("DATABASE_URL=sqlite:///./nexora_local.db")
    assert not scan_text("NEXORA_FERNET_KEY=<generated_at_runtime>")
    assert not scan_text("DERIV_CLIENT_ID=public_client_identifier")

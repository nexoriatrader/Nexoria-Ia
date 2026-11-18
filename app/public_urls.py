"""Explicit, fail-closed public host routing for the NEXORA migration.

Never trust arbitrary Host/Origin headers as payment redirect destinations.
The legacy Render address remains usable while the Deriv OAuth redirect is migrated.
"""
import os
from urllib.parse import urlsplit

LIVE_ORIGINS = frozenset((
    "https://nexoria-ia.onrender.com",
    "https://www.robonexora.online",
))
LOCAL_ORIGIN = "http://localhost:8000"


def canonical_origin():
    """The currently authorized OAuth/public origin, switched only after Deriv setup."""
    origin = os.getenv("NEXORA_PUBLIC_URL", "https://nexoria-ia.onrender.com").rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme == "https" and origin in LIVE_ORIGINS:
        return origin
    if origin == LOCAL_ORIGIN:
        return origin
    return None


def origin_for_request(request):
    """Map known application hosts to fixed origins, never reflect arbitrary input."""
    host = request.url.hostname
    if host == "nexoria-ia.onrender.com":
        return "https://nexoria-ia.onrender.com"
    if host == "www.robonexora.online":
        return "https://www.robonexora.online"
    if host in ("localhost", "127.0.0.1", "testserver") and canonical_origin() == LOCAL_ORIGIN:
        return LOCAL_ORIGIN
    return None


def checkout_return_origin(request):
    """Stripe Checkout redirects back to the same registered application host."""
    return origin_for_request(request)


def nexora_referral_origin():
    """Migrate referral URLs when the canonical OAuth login is ready."""
    return canonical_origin()

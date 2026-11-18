"""New domain migration: legacy compatibility and fixed-origin security."""
import asyncio
from urllib.parse import parse_qs, urlsplit
from starlette.requests import Request
from app import public_urls, main
from pathlib import Path


def request_for(host):
    async def recv():
        return {"type": "http.request", "body": b"", "more_body": False}
    return Request({"type": "http", "scheme": "https",
                    "server": (host, 443),
                    "headers": [(b"host",host.encode())],
                    "path": "/", "query_string": b"",
                    "method": "GET"}, receive=recv)


def test_only_known_hosts_can_generate_stripe_redirects(monkeypatch):
    monkeypatch.setenv("NEXORA_PUBLIC_URL","https://nexoria-ia.onrender.com")
    assert public_urls.checkout_return_origin(request_for("www.robonexora.online")) == "https://www.robonexora.online"
    assert public_urls.checkout_return_origin(request_for("nexoria-ia.onrender.com")) == "https://nexoria-ia.onrender.com"
    assert public_urls.checkout_return_origin(request_for("attacker.example")) is None
    assert public_urls.nexora_referral_origin() == "https://nexoria-ia.onrender.com"
    monkeypatch.setenv("NEXORA_PUBLIC_URL","https://www.robonexora.online")
    assert public_urls.nexora_referral_origin() == "https://www.robonexora.online"
    assert public_urls.canonical_origin() == "https://www.robonexora.online"


def test_oauth_start_redirects_to_canonical_before_creating_cookie(monkeypatch):
    monkeypatch.setenv("NEXORA_PUBLIC_URL","https://nexoria-ia.onrender.com")
    response=asyncio.run(main.login_start(request_for("www.robonexora.online"),ref="ABCDEFGH"))
    assert response.status_code==302
    assert response.headers["location"] == "https://nexoria-ia.onrender.com/auth/deriv/start?ref=ABCDEFGH"
    assert "set-cookie" not in response.headers
    monkeypatch.setenv("NEXORA_PUBLIC_URL","https://www.robonexora.online")
    response=asyncio.run(main.login_start(request_for("nexoria-ia.onrender.com"),ref="ABCDEFGH"))
    assert response.headers["location"] == "https://www.robonexora.online/auth/deriv/start?ref=ABCDEFGH"


def test_deriv_affiliate_and_nexora_referrals_are_distinct():
    html=(Path(__file__).resolve().parents[1]/"app"/"index.html").read_text()
    assert "https://t.deriv.link/?t=HR6MZB6Q499Z" in html
    assert "(a.referral_base_url||location.origin)+'/i/'" in html
    assert "Acessar Corretora" in html

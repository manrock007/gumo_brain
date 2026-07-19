"""Epic G1 — GitHub App per-repo installation tokens, PAT fallback."""

import asyncio
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import githubapp
from app.config import Settings


@pytest.fixture()
def rsa_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture(autouse=True)
def _reset():
    githubapp._reset_caches()
    yield
    githubapp._reset_caches()


def _app_settings(rsa_pem):
    return Settings(github_app_id="12345", github_app_private_key=rsa_pem,
                    github_token="pat-fallback")


def _transport(handler):
    return httpx.MockTransport(handler)


def test_app_jwt_signed(rsa_pem):
    s = _app_settings(rsa_pem)
    token = githubapp.app_jwt(s)
    key = serialization.load_pem_private_key(rsa_pem.encode(), password=None)
    decoded = jwt.decode(token, key.public_key(), algorithms=["RS256"],
                         options={"verify_exp": True})
    assert decoded["iss"] == "12345"
    assert decoded["exp"] - decoded["iat"] <= 600


def test_mint_token_and_cache(rsa_pem):
    s = _app_settings(rsa_pem)
    calls = {"install": 0, "token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            calls["install"] += 1
            return httpx.Response(200, json={"id": 999})
        if "access_tokens" in request.url.path:
            calls["token"] += 1
            exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
            return httpx.Response(201, json={"token": "ghs_installtoken", "expires_at": exp})
        return httpx.Response(404)

    t = _transport(handler)
    tok, kind = asyncio.run(githubapp.effective_git_token(s, "acme/demo", transport=t))
    assert tok == "ghs_installtoken" and kind == "app"
    # second call hits the token cache, no new mint
    tok2, _ = asyncio.run(githubapp.effective_git_token(s, "acme/demo", transport=t))
    assert tok2 == "ghs_installtoken"
    assert calls["token"] == 1


def test_refresh_when_near_expiry(rsa_pem):
    s = _app_settings(rsa_pem)
    githubapp._token_cache["acme/demo"] = ("old", time.time() + 10)  # within slack window

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 1})
        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        return httpx.Response(201, json={"token": "fresh", "expires_at": exp})

    tok, _ = asyncio.run(githubapp.effective_git_token(s, "acme/demo", transport=_transport(handler)))
    assert tok == "fresh"


def test_fallback_to_pat_when_app_off():
    s = Settings(github_token="pat-only")
    tok, kind = asyncio.run(githubapp.effective_git_token(s, "acme/demo"))
    assert tok == "pat-only" and kind == "pat"


def test_fallback_to_pat_on_no_installation(rsa_pem):
    s = _app_settings(rsa_pem)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)  # app not installed on this repo

    tok, kind = asyncio.run(githubapp.effective_git_token(s, "acme/demo", transport=_transport(handler)))
    assert tok == "pat-fallback" and kind == "pat"


def test_fallback_to_pat_on_app_error():
    # invalid private key -> app_jwt raises -> fall back to PAT, never raise
    s = Settings(github_app_id="1", github_app_private_key="not-a-pem", github_token="pat-x")
    tok, kind = asyncio.run(githubapp.effective_git_token(s, "acme/demo"))
    assert tok == "pat-x" and kind == "pat"


def test_token_never_in_error_repr(rsa_pem):
    """effective_git_token must never surface app internals; on error it returns
    the PAT tuple, never raises with token material."""
    s = _app_settings(rsa_pem)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    tok, kind = asyncio.run(githubapp.effective_git_token(s, "acme/demo", transport=_transport(handler)))
    assert kind == "pat"

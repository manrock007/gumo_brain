"""GitHub App auth (Epic G1) — per-repo short-lived installation tokens.

Additive to the PAT: when GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY are configured
the engine mints a fresh 1-hour installation token scoped to the single repo a
run touches, and hands ONLY that token to the subprocess (never the PAT, never
the private key). A repo the app cannot reach, or any app error, falls back to
the PAT (fail-open to the working path) — the app is never a hard replacement.

The private key is resolved through secrets.read_secret (so it supports the
`@/path/to/key.pem` mounted-file convention) and is used ONLY to sign the short
app JWT here. It never enters any subprocess environment.

A test seam: every network call accepts an injected httpx transport so the
suite drives the flow with scripted responses and no network.
"""

import logging
import os
import time

import httpx
import jwt

from .secrets import read_secret

log = logging.getLogger("brain.githubapp")

# in-process caches (single-process SQLite deployment). Postgres/multi-worker
# would just re-mint per process — installation tokens are cheap and short.
_install_id_cache: dict[str, int] = {}          # repo -> installation id
_token_cache: dict[str, tuple[str, float]] = {}  # repo -> (token, expires_at)


def _api_base(settings) -> str:
    # GHES support: honor GITHUB_API_URL (also in the subprocess allow-list).
    return (os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")


def app_jwt(settings) -> str:
    """RS256 JWT signed with the app private key. iss=app id, ≤10-min life.
    Raises on a missing/invalid key — callers treat that as 'app unavailable'."""
    pem = read_secret(settings, settings.github_app_private_key)
    if not pem:
        raise RuntimeError("github app private key unavailable")
    now = int(time.time())
    payload = {"iat": now - 30, "exp": now + 540, "iss": str(settings.github_app_id)}
    return jwt.encode(payload, pem, algorithm="RS256")


async def _get(url: str, headers: dict, transport=None) -> httpx.Response:
    async with httpx.AsyncClient(transport=transport, timeout=30) as client:
        return await client.get(url, headers=headers)


async def _post(url: str, headers: dict, json: dict, transport=None) -> httpx.Response:
    async with httpx.AsyncClient(transport=transport, timeout=30) as client:
        return await client.post(url, headers=headers, json=json)


async def installation_id_for_repo(settings, repo: str, transport=None) -> int | None:
    """GET /repos/{repo}/installation with the app JWT (cached per repo)."""
    if repo in _install_id_cache:
        return _install_id_cache[repo]
    headers = {"Authorization": f"Bearer {app_jwt(settings)}",
               "Accept": "application/vnd.github+json"}
    resp = await _get(f"{_api_base(settings)}/repos/{repo}/installation", headers, transport)
    if resp.status_code != 200:
        log.info("no app installation for %s (HTTP %s)", repo, resp.status_code)
        return None
    inst_id = int(resp.json()["id"])
    _install_id_cache[repo] = inst_id
    return inst_id


async def mint_installation_token(settings, repo: str, transport=None) -> str | None:
    """POST an installation access token scoped to `repo` (1h), cached and
    refreshed a few minutes early. Returns None when the app can't reach the
    repo — the caller falls back to the PAT."""
    cached = _token_cache.get(repo)
    slack = settings.github_app_token_refresh_slack_seconds
    if cached and cached[1] - slack > time.time():
        return cached[0]
    inst_id = await installation_id_for_repo(settings, repo, transport)
    if inst_id is None:
        return None
    headers = {"Authorization": f"Bearer {app_jwt(settings)}",
               "Accept": "application/vnd.github+json"}
    name = repo.split("/")[-1]
    resp = await _post(f"{_api_base(settings)}/app/installations/{inst_id}/access_tokens",
                       headers, {"repositories": [name]}, transport)
    if resp.status_code not in (200, 201):
        log.warning("installation token mint for %s failed: HTTP %s", repo, resp.status_code)
        return None
    data = resp.json()
    token = data.get("token")
    if not token:
        return None
    # parse the RFC3339 expiry; on any parse trouble use a conservative 55min
    expires_at = time.time() + 3300
    exp_raw = data.get("expires_at")
    if exp_raw:
        try:
            from datetime import datetime
            expires_at = datetime.fromisoformat(exp_raw.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            pass
    _token_cache[repo] = (token, expires_at)
    return token


async def effective_git_token(settings, repo: str, transport=None) -> tuple[str, str]:
    """(token, kind) for authenticating git/gh against `repo`.

    Installation token ('app') when the app is configured AND covers the repo;
    otherwise the PAT ('pat'). ANY app error falls back to the PAT — logged,
    never raised — so a misconfigured app never strands a run."""
    if getattr(settings, "github_app_enabled", False):
        try:
            token = await mint_installation_token(settings, repo, transport)
            if token:
                return token, "app"
        except Exception as e:  # fail-open to the PAT, never leak details
            log.warning("github app token path failed for %s: %s", repo, type(e).__name__)
    return settings.github_token, "pat"


def _reset_caches():  # test helper
    _install_id_cache.clear()
    _token_cache.clear()

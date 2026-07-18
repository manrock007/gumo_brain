"""Users, roles and sessions (docs/ENGINE.md §11).

Two roles: admin (configures the instance) and member (does the work in
assigned workspaces — assignment lands in Phase 2; in Phase 1 members see
everything but cannot change configuration). Two ways in, same users table:

- Browser: POST /api/login sets an HttpOnly cookie whose 256-bit random token
  is stored HASHED in auth_sessions (a DB leak exposes no usable tokens).
- Automation: per-user HTTP Basic on any API route (curl keeps working).

First boot bootstraps an admin from CTRLLOOP_ADMIN_PASSWORD; if only the
legacy DASHBOARD_PASSWORD is set, the admin is created as user "gumo" with
that password so existing deployments upgrade without a credentials change.
"""

import base64
import hashlib
import logging
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request

from .config import Settings
from .db import JobStore

log = logging.getLogger("brain.auth")

SESSION_COOKIE = "ctrlloop_session"
_hasher = PasswordHasher()  # argon2id, library defaults

# Sentinel pw_hash for SSO-provisioned accounts: a value argon2.verify can never
# match, so an SSO-only user has NO usable local password (Epic E1). It is not a
# valid argon2 encoding, so _verify_hash returns False for any input.
UNUSABLE_PW = "!sso-no-local-password!"


def is_local_provider(user: dict) -> bool:
    return (user.get("auth_provider") or "local") == "local"


def has_usable_password(user: dict) -> bool:
    """A local account with a real argon2 hash (not the SSO sentinel)."""
    return bool(user.get("pw_hash")) and user.get("pw_hash") != UNUSABLE_PW


def is_break_glass(user: dict) -> bool:
    """The break-glass predicate (Epic E1/E2, amendment blocker 7): a
    local-provider user with a usable password can ALWAYS password-login — the
    credential is never invalidated for SSO reasons, so the instance can never
    lock itself out of local auth."""
    return is_local_provider(user) and has_usable_password(user)


def _is_instance_admin(user: dict) -> bool:
    # E3 renames 'admin' -> 'instance_admin'; accept both during transition.
    return (user.get("role") or "") in ("admin", "instance_admin")


def basic_deprecation_exempt(user: dict) -> bool:
    """Who may still use HTTP Basic AFTER minting a token (Epic E2). Regular
    accounts are nudged to their ctl_ token for automation; break-glass
    instance admins (local provider, usable password) stay allowed so an ops
    user is never locked out even after tokening. Faithful to blocker 7's core
    guarantee — the admin can always get in — while the deprecation still bites
    for non-admin automation accounts."""
    return is_break_glass(user) and _is_instance_admin(user)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def _verify_hash(pw_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(pw_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:  # malformed hash — treat as failure, never crash auth
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def bootstrap_admin(store: JobStore, settings: Settings):
    """Create the first admin when the users table is empty."""
    if store.user_count() > 0:
        return
    if settings.ctrlloop_admin_password:
        username, password = settings.ctrlloop_admin_user.strip() or "admin", \
            settings.ctrlloop_admin_password
    elif settings.dashboard_password:
        # LEGACY (deliberate, kept for upgrade compat — ENGINE.md §11): the
        # pre-CtrlLoop single-credential era bootstraps its admin as user
        # "gumo" so existing deployments keep their credentials unchanged.
        username, password = "gumo", settings.dashboard_password
    else:
        log.warning("no users and no CTRLLOOP_ADMIN_PASSWORD/DASHBOARD_PASSWORD set — "
                    "dashboard and API are unusable until one is provided")
        return
    store.user_create(username, hash_password(password), role="admin",
                      must_change_pw=False)
    log.info("bootstrapped admin user '%s'", username)


def verify_login(store: JobStore, settings: Settings,
                 username: str, password: str) -> dict:
    """Username/password -> user row. Raises HTTPException on any failure;
    the message never distinguishes unknown-user from wrong-password."""
    generic = HTTPException(status_code=401, detail="invalid credentials")
    user = store.user_get(username)
    if user is None:
        _verify_hash(_DUMMY_HASH, password)  # constant-ish time: hash anyway
        raise generic
    if user.get("disabled"):
        raise generic
    if (user.get("locked_until") or 0) > time.time():
        raise HTTPException(status_code=429,
                            detail="account temporarily locked — try again later")
    # Epic E1: an SSO-only account (non-local provider, no usable local
    # password) can never password-auth — refused with the generic 401 so it
    # cannot be distinguished from a wrong password. A local-provider account
    # with a usable password is the break-glass path and is always honored.
    if not is_local_provider(user) and not has_usable_password(user):
        _verify_hash(_DUMMY_HASH, password)
        raise generic
    if not _verify_hash(user["pw_hash"], password):
        store.user_record_failure(username, settings.auth_lockout_attempts,
                                  settings.auth_lockout_seconds)
        raise generic
    if user.get("failed_attempts"):
        store.user_set(username, failed_attempts=0)
    return user


_DUMMY_HASH = hash_password("ctrlloop-timing-dummy")


class SSOConflict(Exception):
    """An SSO login's email/username matches an existing LOCAL account. We NEVER
    auto-adopt a local account (that could hijack an admin's break-glass); the
    conflict is surfaced for manual linking instead."""


def jit_provision(store: JobStore, settings: Settings, provider, claims: dict) -> dict:
    """Resolve an SSO login to a CtrlLoop user, creating one on first sight.

    Auto-link ONLY on the IdP-stable external_id (Epic E1, blocker 6): an
    email/username collision with a LOCAL account is a conflict, never an
    adoption — we must never overwrite a local user's pw_hash/auth_provider,
    which would kill their break-glass password or silently take over the admin.
    On repeat login, role-sync (OIDC_ROLE_SYNC) re-maps the role EXCEPT it never
    demotes the last enabled instance_admin and never touches a local-provider
    admin at all."""
    external_id = (claims.get("external_id") or "").strip()
    if not external_id:
        raise SSOConflict("SSO claim has no stable subject id (sub)")
    existing = store.user_by_external_id(provider.name, external_id)
    role = provider.jit_role(claims, settings)
    if existing is not None:
        _maybe_role_sync(store, existing, role, settings)
        return store.user_get(existing["username"])
    # No external_id match. A local account with the same email/username must
    # NOT be adopted — surface the conflict for an admin to link deliberately.
    email = (claims.get("email") or "").strip()
    username = (claims.get("username") or "").strip() or email or external_id
    for candidate in (username, email):
        if not candidate:
            continue
        clash = store.user_get(candidate)
        if clash is not None and (clash.get("auth_provider") or "local") == "local":
            raise SSOConflict(
                f"an existing local account '{clash['username']}' matches this SSO "
                f"identity — an admin must link it (never auto-adopted)")
    # unique the username against any non-local collision
    final_username = username
    if store.user_get(final_username) is not None:
        final_username = f"{username}-{external_id[:6]}"
    user = store.user_create(final_username, UNUSABLE_PW, role=role,
                             must_change_pw=False)
    store.user_set(final_username, auth_provider=provider.name,
                   external_id=external_id, email=email)
    log.info("jit-provisioned SSO user '%s' (provider=%s, role=%s)",
             final_username, provider.name, role)
    return store.user_get(final_username)


def _maybe_role_sync(store: JobStore, user: dict, new_role: str, settings: Settings):
    if not settings.oidc_role_sync or new_role == user.get("role"):
        return
    # never demote a local-provider admin via SSO role-sync
    if (user.get("auth_provider") or "local") == "local":
        return
    demoting = _is_instance_admin(user) and new_role != "instance_admin"
    if demoting and store.instance_admin_count(enabled_only=True) <= 1:
        log.warning("SSO role-sync refused: would demote the last instance_admin '%s'",
                    user["username"])
        return
    store.user_set(user["username"], role=new_role)


def issue_session(store: JobStore, settings: Settings, user: dict) -> str:
    """New cookie token for a verified user; only its hash is stored."""
    token = secrets.token_urlsafe(32)
    store.auth_session_create(_token_hash(token), user["id"],
                              settings.auth_session_ttl_days * 86400)
    return token


def revoke_session(store: JobStore, token: str):
    store.auth_session_delete(_token_hash(token))


def _basic_credentials(request: Request) -> tuple[str, str] | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return None
    try:
        raw = base64.b64decode(header[6:], validate=True).decode()
        username, _, password = raw.partition(":")
        return username, password
    except Exception:
        return None


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return None


def current_user(request: Request) -> dict | None:
    """Resolve the acting user. An explicit Authorization header is a deliberate
    credential and takes precedence over ambient cookies — and if it is wrong,
    the request FAILS (never silently falls through to the cookie).

    Precedence (Epic E2):
      1. `Authorization: Bearer ctl_…` -> API token; miss/expired/revoked -> 401.
      2. `Authorization: Bearer <other>` -> reserved (OIDC access tokens); 401.
      3. `Authorization: Basic` -> password. DEPRECATION: once the account has an
         active API token, Basic password auth is refused (403) UNLESS the user
         is a break-glass account (local provider + usable password) so an ops
         user can always get in.
      4. Cookie session.
    Returns None only when no credential was presented at all."""
    store: JobStore = request.app.state.store
    settings: Settings = request.app.state.settings
    bearer = _bearer_token(request)
    if bearer is not None:
        if bearer.startswith("ctl_"):
            user = store.api_token_verify(bearer)
            if user is None:
                raise HTTPException(status_code=401, detail="invalid or expired API token",
                                    headers={"WWW-Authenticate": "Bearer"})
            return user
        raise HTTPException(status_code=401, detail="unsupported bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    creds = _basic_credentials(request)
    if creds:
        user = verify_login(store, settings, creds[0], creds[1])  # raises on failure
        if store.user_has_active_token(user["id"]) and not basic_deprecation_exempt(user):
            raise HTTPException(
                status_code=403,
                detail="password auth is disabled for this account — use an API token (ctl_…)")
        return user
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        return store.auth_session_user(_token_hash(token))
    return None


def require_user(request: Request) -> dict:
    """FastAPI dependency: any signed-in, enabled user."""
    if request.app.state.store.user_count() == 0:
        raise HTTPException(status_code=503,
                            detail="no users configured — set CTRLLOOP_ADMIN_PASSWORD and restart")
    user = current_user(request)
    if user is None:
        # WWW-Authenticate kept so curl -u prompts; the SPA routes API 401s
        # to the login page itself
        raise HTTPException(status_code=401, detail="unauthorized",
                            headers={"WWW-Authenticate": "Basic"})
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return user

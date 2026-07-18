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
    if not _verify_hash(user["pw_hash"], password):
        store.user_record_failure(username, settings.auth_lockout_attempts,
                                  settings.auth_lockout_seconds)
        raise generic
    if user.get("failed_attempts"):
        store.user_set(username, failed_attempts=0)
    return user


_DUMMY_HASH = hash_password("ctrlloop-timing-dummy")


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


def current_user(request: Request) -> dict | None:
    """Resolve the acting user. An explicit Authorization header is a
    deliberate credential and takes precedence over ambient cookies — and if
    it is wrong, the request FAILS (verify_login's 401/429 propagates).
    Swallowing that error and falling back to the cookie would let a script
    with bad or revoked credentials silently act as whatever browser session
    shares the cookie jar, and would mask a lockout (429) as a generic 401.
    Returns None only when no credential was presented at all."""
    store: JobStore = request.app.state.store
    settings: Settings = request.app.state.settings
    creds = _basic_credentials(request)
    if creds:
        return verify_login(store, settings, creds[0], creds[1])  # raises on failure
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

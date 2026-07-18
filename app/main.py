import asyncio
import html
import json
import logging
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel

from .auth import (
    SESSION_COOKIE,
    bootstrap_admin,
    current_user,
    hash_password,
    issue_session,
    require_admin,
    require_user,
    revoke_session,
    verify_login,
)
from .chatstream import ChatBroker
from .config import ENGINE_NAME, Settings, get_settings, validate_repo_map
from .db import JobStore
from .feature_prompts import stage_name
from .fixer import ensure_session_store
from .memory import MemoryReader
from .sentry_api import extract_issue_ref, verify_signature
from . import audit, autonomy, inboxlib, people, roles, routines
from .worker import GateConflict, GateForbidden, Worker
from .workspaces import WorkspaceError, WorkspaceNotFound, WorkspaceService
from . import transcripts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("brain")

settings = get_settings()

ISSUE_URL_RE = re.compile(r"/issues/(\d+)")
SHORT_ID_RE = re.compile(r"^[A-Z][A-Z0-9_-]*-[A-Z0-9]+$")
# https://app.clickup.com/t/86abcd123 or /t/<team_id>/CUSTOM-123, or a bare task id
CLICKUP_URL_RE = re.compile(r"app\.clickup\.com/t/(?:\d+/)?([A-Za-z0-9_-]+)")
CLICKUP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,}$")
USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,31}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    if settings.session_persistence:
        ensure_session_store(settings)
    store = JobStore(settings.db_path)
    overrides = store.config_all()
    try:  # operator-saved project context (repos, business context) wins over env/defaults
        settings.apply_runtime_overrides(overrides)
    except ValueError as e:
        # apply is atomic, so nothing was changed; name the actual faulty source —
        # with no stored overrides the canonical-in-map check is judging the
        # env/code config itself
        source = "stored overrides (app_config)" if overrides else "env/code config"
        log.error("project context invalid in %s: %s — overrides NOT applied; "
                  "fix via PUT /api/context (or the env) and restart", source, e)
    app.state.settings = settings  # auth dependencies resolve via app.state
    bootstrap_admin(store, settings)
    store.auth_sessions_prune()
    workspaces = WorkspaceService(store, settings)
    workspaces.ensure_default()  # upgrade path: wrap the §10 context into a workspace
    app.state.workspaces = workspaces
    # loud warnings for half-configured integrations: defaults are neutral now,
    # so a pre-existing instance that relied on the old baked-in values must
    # set them as env vars (MIGRATION-CTRLLOOP.md upgrade checklist)
    if settings.sentry_auth_token and not settings.sentry_org:
        log.warning("SENTRY_AUTH_TOKEN is set but SENTRY_ORG is empty — the whole "
                    "Sentry lane (webhooks, sweep, manual triggers) is DISABLED. "
                    "Set SENTRY_ORG (and SENTRY_API_BASE for EU-region orgs).")
    if settings.sentry_org and not settings.sentry_auth_token:
        log.warning("SENTRY_ORG is set but SENTRY_AUTH_TOKEN is empty — the whole "
                    "Sentry lane is DISABLED until the token is provided.")
    if settings.clickup_token and not settings.clickup_list_id and \
            not any((w.get("clickup_list_id") or "").strip() for w in store.workspace_list()):
        log.warning("CLICKUP_TOKEN is set but no list id is configured anywhere "
                    "(CLICKUP_LIST_ID or a per-workspace list) — the ClickUp "
                    "integration is DISABLED.")
    # Epic G2: a loud, non-secret line naming the detected model-auth backend
    # so a starved/misconfigured env is visible at boot, not silent mid-run.
    from .secrets import build_subprocess_env, detect_auth_backend
    backend = detect_auth_backend(build_subprocess_env(settings))
    log.info("model auth backend detected: %s", backend)
    # Epic G5: the Anthropic policy line — personal Max OAuth creds must not
    # route OTHER users' requests. Warn loudly (not fatal — break-glass) when a
    # Max token is in use on an instance with more than one enabled user.
    if settings.using_max_oauth_token and store.enabled_user_count() > 1:
        log.warning(
            "POLICY: this instance uses personal Claude Max OAuth credentials "
            "(CLAUDE_CODE_OAUTH_TOKEN) but has >1 enabled user. Anthropic policy "
            "prohibits routing other users' requests through personal Max "
            "credentials — flip to ANTHROPIC_API_KEY (unset CLAUDE_CODE_OAUTH_TOKEN) "
            "before onboarding a second user. See OPERATIONS.md 'Model billing'.")
    # first-run wizard (§14): a deployment that has already processed jobs is
    # not a first run — never greet an upgrading operator with onboarding
    if "setup_done" not in store.config_all() and store.job_count() > 0:
        store.config_set("setup_done", "1")
    worker = Worker(settings, store)
    worker.workspaces = workspaces
    worker.engine.workspaces = workspaces
    worker.engine.memory.canonical_resolver = workspaces.canonical_for
    # Epic I1: sweep/reaper/janitor now ride the routine scheduler (builtin
    # rows, settings-derived cadence, EVERY-boot settle bump). The shepherd
    # and the other control-flow-adjacent loops stay native.
    routines.ensure_seeds(store, settings)
    scheduler = routines.RoutineScheduler(settings, store, worker, workspaces)
    tasks = [
        asyncio.create_task(worker.run_forever()),
        asyncio.create_task(worker.poll_clickup_forever()),
        asyncio.create_task(scheduler.run_forever()),
        asyncio.create_task(worker.shepherd_forever()),
        asyncio.create_task(worker.sla_forever()),
        asyncio.create_task(worker.watch_forever()),
        asyncio.create_task(worker.autonomy_forever()),
        asyncio.create_task(worker.slack_ingest_forever()),
    ]
    app.state.store = store
    app.state.worker = worker
    app.state.scheduler = scheduler
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title=ENGINE_NAME, lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "queued": app.state.worker.queue.qsize()}


# The UI lives in app/static (docs/ENGINE.md §11): index behind auth (browser
# sessions redirect to the login page; per-user HTTP Basic still works for
# curl), the login page and assets public. All paths are relative so the app
# survives reverse-proxy prefixes (e.g. /brain/).
STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = (STATIC_DIR / "index.html").read_text()


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if app.state.store.user_count() == 0:
        return HTMLResponse(
            f"<h3>{ENGINE_NAME}: no users configured</h3>"
            "<p>Set <code>CTRLLOOP_ADMIN_PASSWORD</code> (or the legacy "
            "<code>DASHBOARD_PASSWORD</code>) and restart.</p>", status_code=503)
    if current_user(request) is None:
        return RedirectResponse("login")  # relative: survives proxy prefixes
    # brand subtitle follows the configured product name (docs/ENGINE.md §10);
    # a single-anchor replace keeps the page proxy-prefix-safe
    return _INDEX_HTML.replace(
        "{{product_name}}", html.escape(settings.product_name), 1
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if current_user(request) is not None:
        return RedirectResponse(".")
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/static/style.css")
async def static_css():
    return FileResponse(STATIC_DIR / "style.css", media_type="text/css")


@app.get("/static/app.js")
async def static_js():
    return FileResponse(STATIC_DIR / "app.js", media_type="text/javascript")


# ---------- auth (docs/ENGINE.md §11) ----------


class LoginBody(BaseModel):
    username: str
    password: str


class PasswordBody(BaseModel):
    current: str
    new: str


class UserCreateBody(BaseModel):
    username: str
    password: str
    role: str = "member"


class UserPatchBody(BaseModel):
    role: str | None = None
    disabled: bool | None = None
    password: str | None = None  # admin reset — forces a change at next login
    clickup_user_id: str | None = None  # Epic A1: '' clears; digits link
    # Epic D1 people profile (validated via people.validate_profile):
    person_role: str | None = None      # '' | founder | product | dev | design
    areas: list | None = None           # [{kind: workspace|repo|area, value}]
    authority: list | None = None       # decision-authority tags
    person_notes: str | None = None


def _public_user(u: dict) -> dict:
    return {"username": u["username"], "role": u["role"],
            "disabled": bool(u.get("disabled")),
            "must_change_pw": bool(u.get("must_change_pw")),
            "clickup_user_id": u.get("clickup_user_id") or ""}


@app.post("/api/login")
async def login(body: LoginBody, request: Request, response: Response):
    store: JobStore = app.state.store
    user = verify_login(store, settings, body.username.strip(), body.password)
    token = issue_session(store, settings, user)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax",
        secure=settings.session_cookie_secure,
        max_age=settings.auth_session_ttl_days * 86400, path="/",
    )
    audit.record(store, audit.LOGIN, actor=audit.dashboard_actor(user),
                 actor_kind="user", target=user["username"],
                 channel="dashboard", detail={"method": "password"})
    return _public_user(user)


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        revoke_session(app.state.store, token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
async def me(user: dict = Depends(require_user)):
    return _public_user(user)


@app.post("/api/me/password")
async def change_password(body: PasswordBody, response: Response,
                          user: dict = Depends(require_user)):
    store: JobStore = app.state.store
    verify_login(store, settings, user["username"], body.current)  # re-authenticate
    if len(body.new) < 8:
        raise HTTPException(status_code=400, detail="new password must be at least 8 characters")
    store.user_set(user["username"], pw_hash=hash_password(body.new), must_change_pw=0)
    # rotate, don't strand: revoke EVERY session (any stolen token dies), then
    # re-issue for this browser so the person stays signed in. Dumping them to
    # the login page mid first-sign-in made a successful temp-password change
    # look like a failure — the flag was already cleared, but all they saw was
    # a logout and, on any error, a message rendered into the hidden dashboard.
    store.auth_sessions_revoke_user(user["id"])
    token = issue_session(store, settings, user)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax",
        secure=settings.session_cookie_secure,
        max_age=settings.auth_session_ttl_days * 86400, path="/",
    )
    return {"ok": True, "detail": "password changed — other sessions signed out"}


class TokenCreateBody(BaseModel):
    name: str = ""
    scopes: list | None = None   # reserved; '' = inherit the user's role (full)
    ttl_days: int | None = None  # None = settings default; 0 = no expiry


def _public_token(row: dict) -> dict:
    return {"id": row["id"], "name": row.get("name") or "",
            "prefix_hint": row.get("prefix_hint") or "",
            "scopes": row.get("scopes") or "",
            "created_at": row.get("created_at"),
            "last_used_at": row.get("last_used_at"),
            "expires_at": row.get("expires_at"),
            "revoked_at": row.get("revoked_at")}


@app.post("/api/tokens")
async def create_token(body: TokenCreateBody, user: dict = Depends(require_user)):
    """Mint a scoped API token (Epic E2). Returns the plaintext ONCE."""
    store: JobStore = app.state.store
    ttl = body.ttl_days if body.ttl_days is not None else settings.api_token_default_ttl_days
    if ttl is not None and ttl < 0:
        raise HTTPException(status_code=400, detail="ttl_days must be >= 0 (0 = no expiry)")
    scopes = json.dumps(body.scopes) if body.scopes else ""
    token, row = store.api_token_create(user["id"], name=(body.name or "").strip()[:80],
                                        scopes=scopes, ttl_days=ttl or 0)
    audit.record(store, audit.TOKEN_CREATE, actor=audit.dashboard_actor(user),
                 actor_kind="user", target=str(row["id"]),
                 detail={"name": row.get("name") or "", "ttl_days": ttl or 0})
    return {"token": token, **_public_token(row),
            "warning": "copy this token now — it is shown only once"}


@app.get("/api/tokens")
async def list_tokens(user: dict = Depends(require_user)):
    return [_public_token(r) for r in app.state.store.api_token_list(user["id"])]


@app.delete("/api/tokens/{token_id}")
async def revoke_token(token_id: int, user: dict = Depends(require_user)):
    store: JobStore = app.state.store
    if not store.api_token_revoke(token_id, user["id"]):
        raise HTTPException(status_code=404, detail="token not found")
    audit.record(store, audit.TOKEN_REVOKE, actor=audit.dashboard_actor(user),
                 actor_kind="user", target=str(token_id))
    return {"ok": True}


@app.get("/api/users/{username}/tokens", dependencies=[Depends(require_admin)])
async def admin_list_user_tokens(username: str):
    store: JobStore = app.state.store
    target = store.user_get(username)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    return [_public_token(r) for r in store.api_token_list(target["id"])]


@app.delete("/api/users/{username}/tokens/{token_id}")
async def admin_revoke_user_token(username: str, token_id: int,
                                  admin: dict = Depends(require_admin)):
    """Admin revoke for offboarding (Epic E2)."""
    store: JobStore = app.state.store
    target = store.user_get(username)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    if not store.api_token_revoke(token_id, target["id"]):
        raise HTTPException(status_code=404, detail="token not found")
    audit.record(store, audit.TOKEN_REVOKE, actor=audit.dashboard_actor(admin),
                 actor_kind="user", target=str(token_id),
                 detail={"username": username})
    return {"ok": True}


def _audit_scope_for(user: dict, store: JobStore):
    """Instance admin sees ALL rows (workspace_ids=None); a non-admin
    (workspace-admin export) is scoped to their workspace memberships."""
    if (user.get("role") or "") in ("admin", "instance_admin"):
        return None
    return sorted(store.workspace_ids_for_user(user["id"]))


@app.get("/api/audit")
async def audit_view(limit: int = 100, user: dict = Depends(require_user)):
    """Paged audit viewer (Epic E4). Instance admin sees all; a workspace admin
    sees their workspaces' rows. Members without a workspace-admin scope still
    see only their memberships' rows (read-only)."""
    store: JobStore = app.state.store
    ws_ids = _audit_scope_for(user, store)
    return {"events": store.audit_recent(min(int(limit), 1000), workspace_ids=ws_ids)}


@app.get("/api/audit/export", dependencies=[Depends(require_admin)])
async def audit_export(request: Request, after: int = 0, limit: int = 0):
    """JSONL cursor-paged export for SIEM (Epic E4). One JSON object per line;
    the next cursor is in the X-Next-Cursor header (and re-fetch with ?after=).
    Instance-admin only; a workspace admin's export is membership-scoped."""
    store: JobStore = app.state.store
    user = current_user(request)
    ws_ids = _audit_scope_for(user, store)
    page_size = settings.audit_export_page_size
    n = page_size if limit <= 0 else min(int(limit), page_size)
    rows = store.audit_page(after_id=int(after), limit=n, workspace_ids=ws_ids)
    next_cursor = rows[-1]["id"] if rows else int(after)

    def _lines():
        for r in rows:
            yield json.dumps(r, default=str) + "\n"

    return StreamingResponse(_lines(), media_type="application/x-ndjson",
                             headers={"X-Next-Cursor": str(next_cursor)})


def _decode_profile(row: dict) -> dict:
    return {
        "person_role": row.get("person_role") or "",
        "areas": people._areas_of(row),
        "authority": people._authority_of(row),
        "notes": row.get("notes") or "",
    }


@app.get("/api/users", dependencies=[Depends(require_admin)])
async def users_list():
    store: JobStore = app.state.store
    profiles = {p["id"]: p for p in store.people_all()}
    out = []
    for u in store.user_list():
        p = profiles.get(u["id"], {})
        out.append(u | _decode_profile(p))
    return out


@app.get("/api/people")
async def people_list(user: dict = Depends(require_user)):
    """Who owns what — the member-readable ownership directory (Epic D1).
    Admins see everyone; members see only people whose areas intersect the
    member's OWN workspaces (and only those area entries) — the rest of the
    org's structure never leaks (same 404-posture as workspaces)."""
    store: JobStore = app.state.store
    rows = store.people_all()
    if user.get("role") == "admin":
        return [{"username": p["username"],
                 "person_role": p.get("person_role") or "",
                 "areas": people._areas_of(p),
                 "authority": people._authority_of(p)}
                for p in rows if not p.get("disabled")]
    my_ws = store.workspace_ids_for_user(user["id"])
    ws_slugs = {w["slug"] for w in store.workspace_list() if w["id"] in my_ws}
    repo_slugs = {r["slug"] for r in store.repo_rows_all()
                  if r["workspace_id"] in my_ws}
    out = []
    for p in rows:
        if p.get("disabled"):
            continue
        visible = [a for a in people._areas_of(p)
                   if (a.get("kind") == "workspace" and a.get("value") in ws_slugs)
                   or (a.get("kind") == "repo" and a.get("value") in repo_slugs)]
        if not visible:
            continue
        out.append({"username": p["username"],
                    "person_role": p.get("person_role") or "",
                    "areas": visible,
                    "authority": people._authority_of(p)})
    return out


@app.post("/api/users", dependencies=[Depends(require_admin)])
async def users_create(body: UserCreateBody):
    store: JobStore = app.state.store
    username = body.username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(status_code=400,
                            detail="username: 2-32 chars, letters/digits/._- , starting alphanumeric")
    if body.role not in ("admin", "member"):
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'member'")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="password must be at least 8 characters")
    if store.user_get(username):
        raise HTTPException(status_code=409, detail=f"user '{username}' already exists")
    user = store.user_create(username, hash_password(body.password), role=body.role,
                             must_change_pw=True)
    return _public_user(user)


@app.patch("/api/users/{username}")
async def users_patch(username: str, body: UserPatchBody,
                      admin: dict = Depends(require_admin)):
    store: JobStore = app.state.store
    user = store.user_get(username)
    if user is None:
        raise HTTPException(status_code=404, detail=f"unknown user '{username}'")
    if body.role is not None:
        if body.role not in ("admin", "member"):
            raise HTTPException(status_code=400, detail="role must be 'admin' or 'member'")
        if user["username"] == admin["username"] and body.role != "admin":
            raise HTTPException(status_code=400, detail="you cannot demote yourself")
        store.user_set(username, role=body.role)
    if body.disabled is not None:
        if user["username"] == admin["username"] and body.disabled:
            raise HTTPException(status_code=400, detail="you cannot disable yourself")
        store.user_set(username, disabled=int(body.disabled))
        if body.disabled:
            store.auth_sessions_revoke_user(user["id"])
    if body.password is not None:
        if user["username"] == admin["username"]:
            # the admin reset hands out a TEMPORARY credential (arms
            # must_change_pw) — self-reset loops the forced change forever
            raise HTTPException(status_code=400,
                                detail="use the Account panel to change your own password")
        if len(body.password) < 8:
            raise HTTPException(status_code=400, detail="password must be at least 8 characters")
        store.user_set(username, pw_hash=hash_password(body.password), must_change_pw=1,
                       failed_attempts=0, locked_until=None)
        store.auth_sessions_revoke_user(user["id"])
    if body.clickup_user_id is not None:
        # Epic A1: link/clear a ClickUp identity. One ClickUp id maps to at
        # most ONE user (any state) — read-side 409 for a clean message, and
        # the partial UNIQUE index closes the read-then-write race.
        cu_id = body.clickup_user_id.strip()
        if cu_id and not cu_id.isdigit():
            raise HTTPException(status_code=400,
                                detail="clickup_user_id must be a numeric ClickUp user id")
        if cu_id:
            other = next((u for u in store.user_list()
                          if u.get("clickup_user_id") == cu_id
                          and u["username"] != username), None)
            if other:
                raise HTTPException(
                    status_code=409,
                    detail=f"ClickUp id already linked to '{other['username']}'")
        try:
            store.user_set(username, clickup_user_id=cu_id)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409,
                                detail="ClickUp id already linked to another user")
        # the mapping decides WHOSE ClickUp comments answer role-owned gates
        # (gate-answer authority + attribution) — every link/relink/clear is
        # audited with the acting admin, or an impersonating relink would be
        # unreconstructable afterwards
        old = (user.get("clickup_user_id") or "").strip()
        if cu_id != old:
            store.admin_event_add(
                "clickup_link", target=username,
                detail=f"clickup_user_id: {old or '(none)'} -> {cu_id or '(cleared)'}",
                actor=f"dashboard:{admin['username']}")
    # Epic D1: people profile. Validated BEFORE any write (a bad areas list
    # changes nothing); only-audit-when-changed against the current row —
    # authority tags + areas GRANT routing/decision authority, so the change
    # and its author must be reconstructable (same posture as clickup_link).
    profile_fields = {}
    if body.person_role is not None:
        profile_fields["person_role"] = body.person_role
    if body.areas is not None:
        profile_fields["areas"] = body.areas
    if body.authority is not None:
        profile_fields["authority"] = body.authority
    if body.person_notes is not None:
        profile_fields["notes"] = body.person_notes
    if profile_fields:
        try:
            cleaned = people.validate_profile(profile_fields)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        current = store.person_get(user["id"]) or {
            "person_role": "", "areas": "[]", "authority": "[]", "notes": ""}
        stored = {}
        changed = {}
        for key, value in cleaned.items():
            encoded = json.dumps(value) if key in ("areas", "authority") else value
            stored[key] = encoded
            if (current.get(key) or ("[]" if key in ("areas", "authority") else "")) != encoded:
                changed[key] = value
        if changed:
            store.person_set(user["id"], **stored)
            store.admin_event_add(
                "people_profile", target=username,
                detail=json.dumps(changed, default=str)[:1000],
                actor=f"dashboard:{admin['username']}")
    return _public_user(store.user_get(username))


# ---------- first-run setup (docs/ENGINE.md §14) ----------


@app.get("/api/setup", dependencies=[Depends(require_admin)])
async def setup_status():
    """The first-run checklist: auto-detected live state per onboarding step.
    `needed` flips off once dismissed — and deployments that already processed
    jobs are auto-dismissed at boot, so upgrades never see the wizard."""
    store: JobStore = app.state.store
    reader = MemoryReader(settings)
    live_map = json.loads(settings.repo_map)
    # a fresh install still carries the neutral code defaults — "configured"
    # means the operator actually made them theirs (semantic compare: the
    # workspace migration re-serializes the same default map). allow_empty:
    # the neutral default map IS empty and that must not raise here.
    default_map = validate_repo_map(json.loads(_default_settings.repo_map), allow_empty=True)
    steps = {
        "business_context": settings.business_context != _default_settings.business_context
                            or settings.product_name != _default_settings.product_name,
        "repos": live_map != default_map,
        "github_token": bool(settings.github_token),
        "memory": any(reader.cached(slug)["exists"] for slug in live_map),
        "team": store.user_count() > 1,
    }
    return {"needed": "setup_done" not in store.config_all(), "steps": steps}


@app.post("/api/setup/dismiss", dependencies=[Depends(require_admin)])
async def setup_dismiss():
    app.state.store.config_set("setup_done", "1")
    return {"ok": True}


# ---------- workspaces (docs/ENGINE.md §12) ----------


class WorkspaceCreateBody(BaseModel):
    slug: str
    name: str
    product_name: str | None = None
    workspace_context: str | None = None
    canonical_project: str | None = None
    clickup_list_id: str | None = None
    clickup_enabled: bool | None = None
    slack_webhook_url: str | None = None
    gate_mode_default: str | None = None
    require_attributed_answers: str | None = None  # Epic A1: auto | on | off
    stage_role_map: str | None = None              # Epic A3: JSON overrides; '' = inherit
    gate_sla_hours: int | str | None = None        # Epic A5: '' = inherit instance default
    analytics_provider: str | None = None          # Epic B3: '' = none | 'mixpanel'
    analytics_config: dict | str | None = None     # Epic B3: stored secret; NEVER echoed back
    slack_channels: list | str | None = None       # Epic D3: ingest allowlist; '' clears
    budget_monthly_usd: float | str | None = None  # Epic I4/G4: '' = inherit instance


class WorkspacePatchBody(WorkspaceCreateBody):
    slug: str | None = None  # slugs are immutable; ignored on patch
    name: str | None = None
    repos: list[dict] | None = None  # [{slug, repo, base?, setup_cmd?, test_cmd?, allow?}]


class MemberBody(BaseModel):
    username: str
    member: bool


def _ws_svc() -> WorkspaceService:
    return app.state.workspaces


def _job_scoped(job_id: str, user: dict) -> dict:
    """The job, iff the user may see it — 404 either way so existence of
    other workspaces' jobs never leaks to members."""
    job = app.state.store.get(job_id)
    if job is None or not _ws_svc().user_can_access(user, job.get("workspace_id")):
        raise HTTPException(status_code=404, detail=f"unknown job '{job_id}'")
    return job


def _require_project_access(project: str, user: dict):
    ws = _ws_svc().for_project(project)
    if ws is None or not _ws_svc().user_can_access(user, ws["id"]):
        raise HTTPException(status_code=400, detail=f"unknown project '{project}'")


@app.get("/api/workspaces")
async def workspaces_list(user: dict = Depends(require_user)):
    svc = _ws_svc()
    return [svc.public(w) for w in svc.user_workspaces(user)]


def _audit_workspace_mutation(kind: str, workspace_id, admin: dict, provided: dict):
    """Audit a workspace config mutation with the acting admin (BUILD-PLAN:
    every new mutation is auditable). These keys are security-relevant —
    stage_role_map reassigns gate ownership, require_attributed_answers='off'
    disables ClickUp attribution enforcement — so the change and its author
    must be reconstructable. Secret-bearing values are redacted, never stored
    in the audit row."""
    if not provided:
        return
    redacted = {}
    for k, v in provided.items():
        if k in ("analytics_config", "slack_webhook_url"):
            redacted[k] = "(redacted)"
        elif k == "repos":
            redacted[k] = sorted(str((r or {}).get("slug") or "?") for r in (v or []))
        else:
            redacted[k] = v
    app.state.store.admin_event_add(
        kind, target=str(workspace_id),
        detail=json.dumps(redacted, default=str)[:1000],
        actor=f"dashboard:{admin['username']}")


@app.post("/api/workspaces")
async def workspaces_create(body: WorkspaceCreateBody,
                            admin: dict = Depends(require_admin)):
    svc = _ws_svc()
    try:
        ws = svc.create(body.slug, body.name,
                        **body.model_dump(exclude={"slug", "name"}, exclude_none=True))
    except WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _audit_workspace_mutation("workspace_create", ws["id"], admin,
                              body.model_dump(exclude_none=True))
    return svc.public(ws)


@app.patch("/api/workspaces/{workspace_id}")
async def workspaces_patch(workspace_id: int, body: WorkspacePatchBody,
                           admin: dict = Depends(require_admin)):
    svc = _ws_svc()
    try:
        ws = svc.update(workspace_id, repos=body.repos,
                        **body.model_dump(exclude={"slug", "repos"}, exclude_none=True))
    except WorkspaceNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # recorded only after the validated update landed — a 400 changes nothing
    # and audits nothing
    _audit_workspace_mutation("workspace_config", workspace_id, admin,
                              body.model_dump(exclude={"slug"}, exclude_none=True))
    return svc.public(ws) | {"warning": _live_unmapped_warning(app.state.store)}


@app.put("/api/workspaces/{workspace_id}/members", dependencies=[Depends(require_admin)])
async def workspaces_member(workspace_id: int, body: MemberBody):
    store: JobStore = app.state.store
    if store.workspace_get(workspace_id) is None:
        raise HTTPException(status_code=404, detail="unknown workspace")
    target = store.user_get(body.username)
    if target is None:
        raise HTTPException(status_code=404, detail=f"unknown user '{body.username}'")
    store.workspace_member_set(workspace_id, target["id"], body.member)
    return _ws_svc().public(store.workspace_get(workspace_id))


@app.get("/api/budgets")
async def budgets_view(user: dict = Depends(require_user)):
    """Per accessible workspace spend vs budget (Epic G4). Membership-scoped:
    admins see all, members see their workspaces."""
    from . import budgets as budgets_mod
    store: JobStore = app.state.store
    is_admin = (user.get("role") or "") in ("admin", "instance_admin")
    my_ws = None if is_admin else store.workspace_ids_for_user(user["id"])
    out = []
    for ws in store.workspace_list():
        if my_ws is not None and ws["id"] not in my_ws:
            continue
        st = budgets_mod.budget_status(store, settings, ws)
        out.append({"workspace_id": ws["id"], "slug": ws.get("slug"),
                    "name": ws.get("name") or ws.get("slug"), **st})
    return {"budgets": out, "block_enabled": settings.budget_block_enabled}


@app.get("/api/jobs")
async def jobs(user: dict = Depends(require_user)):
    store: JobStore = app.state.store
    rows = store.recent()
    svc = _ws_svc()
    rows = [r for r in rows if svc.user_can_access(user, r.get("workspace_id"))]
    # autonomy annotation (Epic C3): two prefetches, never an N+1
    scores = {(s["workspace_id"], s["project"], s["stage"]): s
              for s in store.autonomy_scores_all()}
    pins = {(p["workspace_id"], p["stage"]): p["pin"]
            for p in store.autonomy_pins_all()}
    for r in rows:
        if r.get("kind") == "feature":
            stage = int(r.get("stage") or 0)
            r["stage_name"] = stage_name(stage)
            cell = scores.get((r.get("workspace_id"), r.get("project") or "", stage))
            r["autonomy_level"] = int(cell["level"]) if cell else 0
            r["autonomy_pin"] = pins.get((r.get("workspace_id"), stage), "")
    return rows


@app.get("/api/inbox")
async def inbox(user: dict = Depends(require_user)):
    """Per-person work queue (Epic A4): every answerable gate the user owns,
    plus unassigned gates (no enforceable DRI — a solo instance sees
    everything). Sorted overdue first, then oldest gate first. `counts.mine`
    (which includes unassigned) is the badge number."""
    store: JobStore = app.state.store
    svc = _ws_svc()
    now = time.time()
    items = []
    unassigned_n = 0
    for job in store.awaiting_gates():
        if not svc.user_can_access(user, job.get("workspace_id")):
            continue
        # Epic I2: the shared user-independent summary (one implementation of
        # "overdue" for the inbox AND the standup digest); the per-user fields
        # (is_you / mine-filtering) stay here — the digest has no acting user.
        item, owner = inboxlib.gate_summary(store, settings, svc.for_job(job),
                                            job, now)
        enforced = owner is not None and owner.enforce
        is_you = roles.actor_is_owner(owner, user) if enforced else False
        unassigned = not enforced
        if not (unassigned or is_you):
            continue
        if unassigned:
            unassigned_n += 1
        items.append(item | {
            "gate_owner": ({"role": owner.role, "display": owner.display,
                            "is_you": is_you} if enforced else None),
            "unassigned": unassigned,
        })
    items.sort(key=lambda i: (not i["overdue"], i["gate_posted_at"] or 0))
    # Epic D3: parked Slack decision candidates — inbox material awaiting a
    # human confirm/dismiss, membership-scoped, oldest first. Additive keys
    # only: items and counts.mine keep their exact pre-D3 semantics (the
    # badge number never includes candidates).
    ws_ids = None if user.get("role") == "admin" \
        else sorted(store.workspace_ids_for_user(user["id"]))
    candidates = [_decision_public(d) for d in store.decision_candidates(ws_ids, 50)]
    # Epic I0: durable routine outputs — open notices, membership-scoped
    # (workspace_id NULL rows are admin-only, matching the jobs posture).
    # Additive keys only: items/counts.mine keep their exact pre-I semantics.
    notices = [_notice_public(n) for n in store.inbox_items_open(ws_ids)]
    return {"items": items,
            "candidates": candidates,
            "notices": notices,
            "counts": {"mine": len(items), "unassigned": unassigned_n,
                       "overdue": sum(1 for i in items if i["overdue"]),
                       "candidates": len(candidates),
                       "notices": len(notices)}}


def _notice_public(n: dict) -> dict:
    try:
        refs = json.loads(n.get("refs") or "{}")
        if not isinstance(refs, dict):
            refs = {}
    except (ValueError, TypeError):
        refs = {}
    return {k: n.get(k) for k in ("id", "workspace_id", "kind", "source",
                                  "title", "body", "status", "status_by",
                                  "status_at", "created_at", "updated_at")} | {"refs": refs}


def _notice_scoped(item_id: int, user: dict) -> dict:
    """The notice iff the user may see it — 404 either way (no existence
    leaks). NULL-workspace notices are instance-wide → admin-only."""
    item = app.state.store.inbox_item_get(item_id)
    if item is None or not _ws_svc().user_can_access(user, item.get("workspace_id")):
        raise HTTPException(status_code=404, detail="unknown notice")
    return item


class NoticeDismissBody(BaseModel):
    reason: str = ""


class NoticeAdoptBody(BaseModel):
    as_: str = "feature"  # 'feature' | 'task'
    project: str = ""
    title: str | None = None
    request: str | None = None
    founder_dri: str | None = None
    dev_dri: str | None = None
    success_metric: str | None = None
    metric_target: str | None = None
    metric_window_days: int | None = None

    model_config = {"populate_by_name": True}

    def __init__(self, **data):
        if "as" in data:  # accept the design's `as` key (reserved word in Python)
            data.setdefault("as_", data.pop("as"))
        super().__init__(**data)


@app.post("/api/inbox/notices/{item_id}/dismiss")
async def notice_dismiss(item_id: int, body: NoticeDismissBody,
                         user: dict = Depends(require_user)):
    """Dismiss a notice. The row is KEPT (dismissal memory): its dedupe key
    blocks any unchanged re-proposal forever. CAS — 409 to the loser."""
    store: JobStore = app.state.store
    _notice_scoped(item_id, user)
    extra = {"dismiss_reason": body.reason.strip()[:500]} if body.reason.strip() else None
    if not store.inbox_item_resolve(item_id, "dismissed",
                                    f"dashboard:{user['username']}", extra):
        raise HTTPException(status_code=409, detail="notice changed state concurrently")
    return _notice_public(store.inbox_item_get(item_id))


@app.post("/api/inbox/notices/{item_id}/adopt")
async def notice_adopt(item_id: int, body: NoticeAdoptBody,
                       user: dict = Depends(require_user)):
    """Adopt a proposal into the ordinary intake — the ONLY path from a
    proposal to a pipeline (routines never self-initiate one). Validation
    happens BEFORE the CAS resolve, exactly like POST /api/features: project
    must be mapped, non-admins need project access. The brief flows into
    job.request and takes the untrusted-fragment posture of a ClickUp
    description in every stage prompt."""
    store: JobStore = app.state.store
    worker: Worker = app.state.worker
    item = _notice_scoped(item_id, user)
    if (item.get("kind") or "") != "proposal":
        raise HTTPException(status_code=400, detail="only proposals can be adopted")
    as_kind = (body.as_ or "feature").strip().lower()
    if as_kind not in ("feature", "task"):
        raise HTTPException(status_code=400, detail="as must be 'feature' or 'task'")
    try:
        refs = json.loads(item.get("refs") or "{}")
        if not isinstance(refs, dict):
            refs = {}
    except (ValueError, TypeError):
        refs = {}
    project = (body.project or "").strip() or str(refs.get("project") or "").strip()
    if not project or settings.repo_for_project(project) is None:
        raise HTTPException(status_code=400, detail=f"unknown project '{project}'")
    if user["role"] != "admin":
        _require_project_access(project, user)
    if body.metric_window_days is not None and not 1 <= body.metric_window_days <= 365:
        raise HTTPException(status_code=400,
                            detail="metric_window_days must be between 1 and 365")
    title = (body.title or "").strip() or item["title"]
    request_text = (body.request or "").strip() or (item.get("body") or "").strip() \
        or item["title"]
    request_text = f"{request_text}\n\n_adopted from proposal #{item['id']}_"
    prefix = "feat" if as_kind == "feature" else "task"
    job_id = f"{prefix}-prop-{item['id']}"
    # CAS-resolve FIRST (409 to the loser) — after the validation above, the
    # only intake refusal path (a duplicate job id) is unreachable post-CAS,
    # so there is no compensating rollback.
    if not store.inbox_item_resolve(
            item_id, "adopted", f"dashboard:{user['username']}",
            {"adopted_as": job_id, "adopted_kind": as_kind, "project": project}):
        raise HTTPException(status_code=409, detail="notice changed state concurrently")
    try:
        if as_kind == "feature":
            worker.intake_feature(
                job_id, title=title, project=project, request=request_text,
                founder_dri=(body.founder_dri or "").strip(),
                dev_dri=(body.dev_dri or "").strip(),
                success_metric=(body.success_metric or "").strip(),
                metric_target=(body.metric_target or "").strip(),
                metric_window_days=body.metric_window_days,
            )
        else:
            worker.intake_task(job_id, title=title, project=project,
                               request=request_text)
        job = store.get(job_id)
        if job is None:
            raise RuntimeError("intake did not create the job row")
    except HTTPException:
        raise
    except Exception as e:
        # visible + auditable: the item stays 'adopted' with the error in
        # refs — never a silent un-adopt (single-writer posture)
        store.inbox_item_merge_refs(item_id, {"intake_error": str(e)[:300]})
        log.exception("proposal adoption intake failed for notice %s", item_id)
        raise HTTPException(status_code=500,
                            detail=f"intake failed after adoption: {str(e)[:200]}")
    return {"job_id": job_id, "kind": as_kind, "project": project,
            "notice": _notice_public(store.inbox_item_get(item_id))}


@app.get("/api/outcomes")
async def outcomes_ledger(user: dict = Depends(require_user)):
    """The outcome ledger (Epic B5): one row per finished watch — metric, goal,
    observed, verdict, learning, decider — membership-scoped (fail closed: rows
    whose workspace the user cannot access simply don't exist here), plus the
    verdict distribution over the visible rows."""
    store: JobStore = app.state.store
    svc = _ws_svc()
    rows = [r for r in store.outcomes_recent(200)
            if svc.user_can_access(user, r.get("workspace_id"))]
    verdicts = {"moved": 0, "flat": 0, "regressed": 0, "unmeasured": 0}
    for r in rows:
        v = r.get("verdict") or "unmeasured"
        verdicts[v] = verdicts.get(v, 0) + 1
    return {"outcomes": rows, "verdicts": verdicts}


# ---------- decision registry (Epic D2, docs/ENGINE.md §16) ----------


DECISION_SCOPES = ("job", "repo", "product", "org")


class DecisionCreateBody(BaseModel):
    scope: str
    text: str
    title: str = ""
    project: str | None = None
    links: list | None = None
    workspace_id: int | None = None


class DecisionPatchBody(BaseModel):
    action: str  # supersede | reactivate


class DecisionConfirmBody(BaseModel):
    scope: str | None = None
    title: str | None = None
    text: str | None = None


def _validate_decision_fields(scope: str | None, text: str | None,
                              title: str | None, links: list | None,
                              user: dict):
    """Shared validation for create AND confirm-with-edits (amendment: the
    exact same rules on both paths). Raises HTTPException."""
    if scope is not None:
        if scope not in DECISION_SCOPES:
            raise HTTPException(status_code=400,
                                detail="scope must be one of: "
                                       + ", ".join(DECISION_SCOPES))
        if scope == "org" and user.get("role") != "admin":
            # org-scope rows reach EVERY member's prompts and registry view —
            # creating/confirming one is an admin action (ENGINE.md §16)
            raise HTTPException(status_code=403,
                                detail="org-scope decisions are admin-only")
    if text is not None:
        if not text.strip():
            raise HTTPException(status_code=400, detail="text must be non-empty")
        if len(text) > 4000:
            raise HTTPException(status_code=400, detail="text is capped at 4000 chars")
    if title is not None and len(title) > 200:
        raise HTTPException(status_code=400, detail="title is capped at 200 chars")
    if links is not None:
        if not all(isinstance(l, str) for l in links):
            raise HTTPException(status_code=400,
                                detail="links must be a list of strings")
        # stored links render as clickable anchors in every member's (and
        # admin's) registry view — whitelist web URLs so a javascript: value
        # can never reach an href (same posture as the dashboard's linkify)
        for l in links:
            u = l.strip().lower()
            if not (u.startswith("https://") or u.startswith("http://")):
                raise HTTPException(status_code=400,
                                    detail="links must be http(s):// URLs")


def _decision_public(d: dict) -> dict:
    try:
        links = json.loads(d.get("links") or "[]")
    except (ValueError, TypeError):
        links = []
    return d | {"links": links}


def _decision_scoped(decision_id: int, user: dict) -> dict:
    """The decision row iff the user may see it — the visibility predicate is
    EXACTLY the prompt-admission predicate: own-workspace rows plus
    scope='org' rows; anything else (incl. workspace_id NULL rows for
    members) is a 404, no existence leaks."""
    row = app.state.store.decision_get(decision_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown decision")
    if user.get("role") != "admin" and row.get("scope") != "org" \
            and not _ws_svc().user_can_access(user, row.get("workspace_id")):
        raise HTTPException(status_code=404, detail="unknown decision")
    return row


@app.post("/api/decisions")
async def decisions_create(body: DecisionCreateBody,
                           user: dict = Depends(require_user)):
    store: JobStore = app.state.store
    _validate_decision_fields(body.scope, body.text, body.title, body.links, user)
    ws_id = None
    if body.workspace_id is not None:
        ws = store.workspace_get(body.workspace_id)
        if ws is None or not _ws_svc().user_can_access(user, ws["id"]):
            raise HTTPException(status_code=400, detail="unknown workspace")
        ws_id = ws["id"]
    elif (body.project or "").strip():
        ws = _ws_svc().for_project(body.project.strip())
        if ws is None or not _ws_svc().user_can_access(user, ws["id"]):
            raise HTTPException(status_code=400,
                                detail=f"unknown project '{body.project}'")
        ws_id = ws["id"]
    if body.scope != "org" and ws_id is None:
        raise HTTPException(status_code=400,
                            detail="provide a workspace_id or project "
                                   "(only org-scope decisions may omit both)")
    did = store.decision_add(
        "manual", body.text.strip(), scope=body.scope,
        title=(body.title or "").strip(), project=(body.project or "").strip(),
        workspace_id=ws_id, links=body.links or [],
        decided_by=f"dashboard:{user['username']}")
    return _decision_public(store.decision_get(did))


@app.get("/api/decisions")
async def decisions_list(q: str = "", scope: str = "", status: str = "",
                         project: str = "", source: str = "",
                         limit: int = 100, offset: int = 0,
                         user: dict = Depends(require_user)):
    """The registry view. Members see their workspaces' rows PLUS every
    org-scope row (org rows reach every member's prompts, so every member may
    read — and ask an admin to supersede — them). Default status view
    excludes candidates (inbox material) and dismissed (remembered
    rejections)."""
    if scope and scope not in DECISION_SCOPES:
        raise HTTPException(status_code=400, detail="unknown scope")
    if status and status not in ("active", "superseded", "candidate", "dismissed"):
        raise HTTPException(status_code=400, detail="unknown status")
    store: JobStore = app.state.store
    ws_ids = None if user.get("role") == "admin" \
        else sorted(store.workspace_ids_for_user(user["id"]))
    rows = store.decisions_query(
        q=q, scope=scope, status=status, project=project, source=source,
        workspace_ids=ws_ids, limit=max(1, min(500, limit)),
        offset=max(0, offset))
    return {"decisions": [_decision_public(d) for d in rows]}


@app.patch("/api/decisions/{decision_id}")
async def decisions_patch(decision_id: int, body: DecisionPatchBody,
                          user: dict = Depends(require_user)):
    store: JobStore = app.state.store
    row = _decision_scoped(decision_id, user)
    if row.get("scope") == "org" and user.get("role") != "admin":
        raise HTTPException(status_code=403,
                            detail="org-scope decisions are admin-only")
    action = body.action.strip().lower()
    if action == "supersede":
        ok = store.decision_set_status(decision_id, ["active"], "superseded",
                                       f"dashboard:{user['username']}")
    elif action == "reactivate":
        ok = store.decision_set_status(decision_id, ["superseded"], "active",
                                       f"dashboard:{user['username']}")
    else:
        raise HTTPException(status_code=400,
                            detail="action must be 'supersede' or 'reactivate'")
    if not ok:
        raise HTTPException(status_code=409,
                            detail="decision changed state concurrently")
    return _decision_public(store.decision_get(decision_id))


@app.post("/api/decisions/{decision_id}/confirm")
async def decisions_confirm(decision_id: int, body: DecisionConfirmBody,
                            user: dict = Depends(require_user)):
    """Confirm a Slack candidate into the registry (Epic D3). The human may
    edit scope/title/text while confirming — validated with EXACTLY the same
    rules as POST /api/decisions (org scope stays admin-only). Confirmation
    stamps decided_by with the ratifying HUMAN; the original Slack author is
    preserved in origin_author (written at ingest) — auditable either way.
    CAS candidate→active; 409 on a lost race. The FTS insert rides the same
    transaction (decision_set_status)."""
    store: JobStore = app.state.store
    row = _decision_scoped(decision_id, user)
    if row.get("status") != "candidate":
        raise HTTPException(status_code=409, detail="not a candidate")
    _validate_decision_fields(body.scope, body.text, body.title, None, user)
    edits = {}
    if body.scope is not None:
        edits["scope"] = body.scope
    if body.title is not None:
        edits["title"] = body.title.strip()
    if body.text is not None:
        edits["text"] = body.text.strip()
    ok = store.decision_set_status(
        decision_id, ["candidate"], "active",
        f"dashboard:{user['username']}",
        decided_by=f"dashboard:{user['username']}", **edits)
    if not ok:
        raise HTTPException(status_code=409,
                            detail="candidate changed state concurrently")
    return _decision_public(store.decision_get(decision_id))


@app.post("/api/decisions/{decision_id}/dismiss")
async def decisions_dismiss(decision_id: int, user: dict = Depends(require_user)):
    """Dismiss a candidate. The row is KEPT (status='dismissed') so its
    (source, ref) key survives — a re-scan can never re-propose it. An
    ordinary work mutation recorded on the row itself (updated_by/updated_at);
    it grants no authority, so no admin_events row."""
    store: JobStore = app.state.store
    row = _decision_scoped(decision_id, user)
    if row.get("status") != "candidate":
        raise HTTPException(status_code=409, detail="not a candidate")
    ok = store.decision_set_status(decision_id, ["candidate"], "dismissed",
                                   f"dashboard:{user['username']}")
    if not ok:
        raise HTTPException(status_code=409,
                            detail="candidate changed state concurrently")
    return _decision_public(store.decision_get(decision_id))


# ---------- graduated autonomy (Epic C, docs/ENGINE.md §15) ----------


class AutonomyPinBody(BaseModel):
    stage: int
    pin: str | None = None  # 'always_gate' | 'always_auto' | null = clear


class AutonomyClawbackBody(BaseModel):
    stage: int
    project: str | None = None  # null = every repo in the workspace


def _pin_map(store: JobStore, workspace_id: int) -> dict:
    return {str(s): {"pin": p["pin"], "set_by": p["set_by"], "set_at": p["set_at"]}
            for s, p in store.autonomy_pins_for(workspace_id).items()}


@app.get("/api/autonomy")
async def autonomy_state(user: dict = Depends(require_user)):
    """The trust-ladder surface: per accessible workspace the stage×repo score
    matrix (inputs JSON-decoded for the UI), the pin map, and the last 50
    autonomy events. Membership-scoped like /api/workspaces."""
    store: JobStore = app.state.store
    svc = _ws_svc()
    out = {"enabled": settings.autonomy_enabled,
           "auto_level": autonomy.effective_auto_level(settings),
           "workspaces": []}
    for ws in svc.user_workspaces(user):
        cells = []
        for r in store.autonomy_scores_for(ws["id"]):
            try:
                inputs = json.loads(r.get("inputs") or "{}")
            except (ValueError, TypeError):
                inputs = {}
            cells.append({
                "project": r["project"], "stage": r["stage"], "level": r["level"],
                "score": r["score"], "sample_runs": r["sample_runs"],
                # display semantics (ENGINE.md §15): the flag shows only while
                # the cell sits at 0 — clawback_at itself is never cleared
                "clawed_back": bool(r.get("clawback_at")) and r["level"] == 0,
                "computed_at": r["computed_at"], "inputs": inputs,
            })
        out["workspaces"].append({
            "id": ws["id"], "slug": ws["slug"], "name": ws["name"],
            "repos": [r["slug"] for r in store.workspace_repos_for(ws["id"])],
            "pins": _pin_map(store, ws["id"]),
            "cells": cells,
            "events": store.autonomy_events_recent([ws["id"]], 50),
        })
    return out


@app.put("/api/workspaces/{workspace_id}/autonomy/pins")
async def autonomy_pin_put(workspace_id: int, body: AutonomyPinBody,
                           admin: dict = Depends(require_admin)):
    """Set or clear a per-workspace per-stage pin (admin — pins are config
    mutations, matching workspace PATCH). Pins always win over computed
    levels and never expire; every change is audited."""
    store: JobStore = app.state.store
    if store.workspace_get(workspace_id) is None:
        raise HTTPException(status_code=404, detail="unknown workspace")
    if not 0 <= body.stage <= 9:
        raise HTTPException(status_code=400, detail="stage must be 0–9")
    pin = (body.pin or "").strip() or None
    actor = f"dashboard:{admin['username']}"
    if pin is None:
        if body.stage in store.autonomy_pins_for(workspace_id):
            store.autonomy_pin_clear(workspace_id, body.stage)
            store.autonomy_event_add("pin_clear", workspace_id=workspace_id,
                                     stage=body.stage,
                                     detail=f"P{body.stage} pin cleared", actor=actor)
    else:
        if pin not in ("always_gate", "always_auto"):
            raise HTTPException(status_code=400,
                                detail="pin must be 'always_gate', 'always_auto' or null")
        if pin == "always_auto" and body.stage == 9:
            raise HTTPException(status_code=400,
                                detail="P9 never auto-advances — its proceed is the "
                                       "terminal transition")
        store.autonomy_pin_set(workspace_id, body.stage, pin, actor)
        store.autonomy_event_add("pin_set", workspace_id=workspace_id, stage=body.stage,
                                 detail=f"P{body.stage} pinned {pin}", actor=actor)
    return {"workspace_id": workspace_id, "pins": _pin_map(store, workspace_id)}


@app.post("/api/workspaces/{workspace_id}/autonomy/clawback")
async def autonomy_clawback_post(workspace_id: int, body: AutonomyClawbackBody,
                                 user: dict = Depends(require_user)):
    """One-click brake. Members of the workspace may pull it (clawback only
    REDUCES autonomy); admins anywhere. 404 on no access — membership-style,
    no existence leaks. project=null claws back every slug that ever held a
    cell here plus the current repo set (stale/moved slugs included)."""
    store: JobStore = app.state.store
    ws = store.workspace_get(workspace_id)
    if ws is None or not _ws_svc().user_can_access(user, workspace_id):
        raise HTTPException(status_code=404, detail="unknown workspace")
    if not 0 <= body.stage <= 9:
        raise HTTPException(status_code=400, detail="stage must be 0–9")
    project = (body.project or "").strip() or None
    if project is not None:
        own = {r["slug"] for r in store.workspace_repos_for(workspace_id)}
        if project not in own:
            raise HTTPException(status_code=400,
                                detail=f"project '{project}' is not a repo slug in "
                                       "this workspace")
    clawed = autonomy.clawback(store, settings, workspace_id, body.stage, project,
                               actor=f"dashboard:{user['username']}")
    return {"clawed": clawed}


@app.post("/api/autonomy/recompute", dependencies=[Depends(require_admin)])
async def autonomy_recompute():
    """Run the autonomy scorer inline (operator/test convenience — nobody
    waits for the nightly pass). Same thread-offload as the loop; the two
    serialize trivially through SQLite's single writer today."""
    if not settings.autonomy_enabled:
        raise HTTPException(status_code=400, detail="autonomy is disabled (AUTONOMY_ENABLED)")
    res = await asyncio.to_thread(
        autonomy.compute, app.state.store, settings)
    return res


# ---------- proactive routines (Epic I1, docs/ENGINE.md §17) ----------


class RoutinePutBody(BaseModel):
    enabled: bool | None = None
    schedule: str | None = None
    config: dict | None = None


def _routine_public(store: JobStore, r: dict, runs: int = 20) -> dict:
    try:
        config = json.loads(r.get("config") or "{}")
        if not isinstance(config, dict):
            config = {}
    except (ValueError, TypeError):
        config = {}
    return {k: r.get(k) for k in ("id", "workspace_id", "kind", "name",
                                  "schedule", "enabled", "last_run_at",
                                  "last_status", "last_result")} | {
        "enabled": bool(r.get("enabled")),
        "config": config,
        "builtin": r.get("workspace_id") is None,
        "runs": store.routine_runs_recent(r["id"], runs),
    }


@app.get("/api/routines")
async def routines_list(user: dict = Depends(require_user)):
    """Membership-scoped routine table + run history (last 20 per routine).
    Instance (builtin) rows are admin-only."""
    store: JobStore = app.state.store
    svc = _ws_svc()
    out = []
    for r in store.routines_all():
        if r.get("workspace_id") is None:
            if user.get("role") != "admin":
                continue
        elif not svc.user_can_access(user, r["workspace_id"]):
            continue
        out.append(_routine_public(store, r))
    return {"routines": out, "enabled": settings.routines_enabled}


@app.put("/api/routines/{routine_id}")
async def routine_put(routine_id: int, body: RoutinePutBody,
                      admin: dict = Depends(require_admin)):
    """Edit a routine (admin). Schedules validate fail-closed (400, nothing
    changes); the reaper row is non-disableable and its schedule is locked;
    every change is audited in admin_events."""
    store: JobStore = app.state.store
    row = store.routine_get(routine_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown routine")
    if row["kind"] == "reaper":
        if body.enabled is False:
            raise HTTPException(status_code=400,
                                detail="the reaper is a safety loop and cannot "
                                       "be disabled")
        if body.schedule is not None:
            raise HTTPException(status_code=400,
                                detail="the reaper's schedule is fixed")
    fields: dict = {}
    if body.schedule is not None:
        spec = body.schedule.strip()
        if spec:
            try:
                sched = routines.parse_schedule(spec)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            if sched.kind in ("daily", "weekly") and row.get("last_run_at") is None:
                # daily/weekly rows anchor next_due on last_run_at — a
                # never-run row must be stamped or it can never come due
                # (same rationale as routines.anchor_fresh_rows)
                fields["last_run_at"] = time.time()
        elif row.get("workspace_id") is not None:
            raise HTTPException(status_code=400,
                                detail="workspace routines need an explicit "
                                       "schedule ('' is builtin-only)")
        fields["schedule"] = spec
    if body.enabled is not None:
        fields["enabled"] = int(body.enabled)
    if body.config is not None:
        if not isinstance(body.config, dict):
            raise HTTPException(status_code=400, detail="config must be an object")
        fields["config"] = json.dumps(body.config)
    if fields:
        store.routine_set(routine_id, **fields)
        diff = {k: (row.get(k), fields[k]) for k in fields}
        store.admin_event_add(
            "routine_config", target=str(routine_id),
            detail=json.dumps({"kind": row["kind"], "changed": diff},
                              default=str)[:1000],
            actor=f"dashboard:{admin['username']}")
    return _routine_public(store, store.routine_get(routine_id))


@app.post("/api/routines/{routine_id}/run")
async def routine_run_now(routine_id: int,
                          admin: dict = Depends(require_admin)):
    """Arm an immediate fire THROUGH the scheduler task (never inline in the
    HTTP request — a sweep does paginated Sentry HTTP). Returns immediately.
    A forced fire is a real mutation (the claim stamps last_run_at; a forced
    sweep enqueues pipelines and spends run budget), so an accepted arm is
    audited in admin_events — routine_runs rows carry no actor and a forced
    firing would otherwise be indistinguishable from a scheduled one."""
    store: JobStore = app.state.store
    row = store.routine_get(routine_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown routine")
    accepted = app.state.scheduler.request_run(routine_id)
    if not accepted:
        raise HTTPException(status_code=400,
                            detail="routine is disabled or its kind is not "
                                   "registered on this build")
    store.admin_event_add(
        "routine_run_now", target=str(routine_id),
        detail=json.dumps({"kind": row["kind"],
                           "workspace_id": row.get("workspace_id")}),
        actor=f"dashboard:{admin['username']}")
    return {"requested": True, "routine_id": routine_id}


class TriggerBody(BaseModel):
    issue: str


@app.post("/api/trigger")
async def trigger(body: TriggerBody, user: dict = Depends(require_user)):
    """Manual fix trigger: Sentry issue id / short id / URL in, ClickUp ticket out."""
    # guard BEFORE any Sentry API call (short-id resolution fires early too):
    # an unconfigured integration is a clean 400, never a masked 404
    if not settings.sentry_enabled:
        raise HTTPException(
            status_code=400,
            detail="Sentry integration is not configured (SENTRY_ORG / SENTRY_AUTH_TOKEN)")
    worker: Worker = app.state.worker
    ref = body.issue.strip()

    issue_id = None
    if ref.isdigit():
        issue_id = ref
    elif m := ISSUE_URL_RE.search(ref):
        issue_id = m.group(1)
    elif SHORT_ID_RE.match(ref.upper()):
        issue_id = await worker.sentry.resolve_short_id(ref.upper())
    if not issue_id:
        raise HTTPException(status_code=400, detail=f"could not parse a Sentry issue from '{ref}'")

    try:
        issue = await worker.sentry.issue(issue_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Sentry issue {issue_id} not found")

    title = issue.get("title", "unknown")
    project = (issue.get("project") or {}).get("slug", "")
    if user["role"] != "admin":
        _require_project_access(project, user)
    decision = worker.intake(issue_id, source="manual", forced=True, title=title, project=project)
    if "queued" not in decision:
        raise HTTPException(status_code=409, detail=decision)

    # create the ticket now so the response includes it (the worker reuses it)
    row = app.state.store.get(issue_id)
    task_url = row.get("clickup_task_url") if row else None
    if row and not row.get("clickup_task_id"):
        cu_on, cu_list = _ws_svc().clickup_route(project)
        created = None
        if cu_on:
            created = await worker.clickup.create_task(
                name=f"[{project}] {title}",
                description=worker._ticket_description(issue, row),
                list_id=cu_list,
            )
        if created:
            task_id, task_url = created
            app.state.store.set_fields(issue_id, clickup_task_id=task_id, clickup_task_url=task_url)

    return {
        "issue_id": issue_id,
        "title": title,
        "project": project,
        "decision": decision,
        "clickup_task_url": task_url,
    }


@app.get("/api/projects")
async def projects(user: dict = Depends(require_user)):
    """Project slugs the user may target, with their workspace (picker + switcher)."""
    svc = _ws_svc()
    out = []
    for ws in svc.user_workspaces(user):
        for slug, entry in sorted(svc.public(ws)["repos"].items()):
            out.append({"slug": slug, "repo": entry.get("repo", ""),
                        "workspace": ws["slug"], "workspace_id": ws["id"]})
    return out


# ---------- project context (docs/ENGINE.md §10) ----------
# What the engine works ON — repos, canonical project, product name, business
# context. Env/code defaults (neutral out of the box) apply until an operator
# saves overrides here; overrides persist in the DB and survive restarts.


class ContextBody(BaseModel):
    product_name: str | None = None
    business_context: str | None = None
    repo_map: dict | None = None        # {slug: {repo, base, setup_cmd?, test_cmd?, allow?}}
    canonical_project: str | None = None  # must be a slug in the (resulting) repo map


# env/code defaults with no runtime overrides, captured once — what DELETE
# restores and what the dashboard shows as "defaults" (env is process-constant)
_default_settings = Settings()


def _live_unmapped_warning(store: JobStore) -> str:
    """Live jobs whose recorded project slug is no longer in the repo map get
    skipped at their next dispatch ('no repo mapped') — surface that at save
    time instead of letting an approved pipeline die silently later."""
    mapping = json.loads(settings.repo_map)
    live = [j for j in store.by_status(["received", "queued", "running", "awaiting_input"])
            if (j.get("project") or "") and j["project"] not in mapping]
    if not live:
        return ""
    listed = ", ".join(f"{j['issue_id']} (project '{j['project']}')" for j in live[:10])
    more = f" and {len(live) - 10} more" if len(live) > 10 else ""
    return (f"warning: live jobs reference project slugs that are no longer mapped and "
            f"will be SKIPPED at their next run: {listed}{more}. Re-add the slug(s) or "
            f"finish those jobs first.")


def _context_payload(warning: str = "") -> dict:
    from .config import RUNTIME_CONTEXT_KEYS
    # only project-context keys are "overridden" — internal app_config markers
    # (setup_done, audit_legacy_copied, …) are not context overrides.
    overridden = sorted(k for k in app.state.store.config_all()
                        if k in RUNTIME_CONTEXT_KEYS)
    return {
        "context": settings.project_context(),
        "defaults": _default_settings.project_context(),
        "overridden": overridden,
        "warning": warning,
    }


@app.get("/api/context", dependencies=[Depends(require_user)])
async def get_context():
    return _context_payload()


@app.put("/api/context", dependencies=[Depends(require_admin)])
async def put_context(body: ContextBody):
    """Update the project context. Only supplied fields change; values are
    validated atomically (a bad payload changes nothing), applied to the live
    engine immediately, and persisted for future restarts. Jobs keep their
    recorded project slug: if the new map still contains it, they continue
    under the new mapping; if a slug was REMOVED, those jobs are skipped at
    their next run — the response carries a warning listing them."""
    if body.repo_map is not None or body.canonical_project is not None:
        raise HTTPException(
            status_code=400,
            detail="repos and the canonical project are managed per workspace now — "
                   "use PATCH /api/workspaces/{id} (Settings → Workspaces)")
    overrides = {
        "product_name": body.product_name,
        "business_context": body.business_context,
    }
    try:
        staged = settings.stage_runtime_overrides(overrides)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    store: JobStore = app.state.store
    # persist BEFORE touching live state, in one transaction: if the write
    # fails, the request 500s with settings unchanged — live and persisted
    # state never diverge (apply_staged cannot fail after validation)
    store.config_set_many({
        key: json.loads(value) if key == "repo_map" else value  # store one-layer JSON
        for key, value in staged.items()
    })
    settings.apply_staged(staged)
    return _context_payload(warning=_live_unmapped_warning(store))


@app.delete("/api/context", dependencies=[Depends(require_admin)])
async def reset_context():
    """Drop every stored override — revert the INSTANCE-owned fields to env/code
    defaults. Repo topology belongs to workspaces (§12): resetting must never
    clobber the workspace-merged map, and we re-sync it to be safe (finding
    1595745/0 — a default-map settings.repo_map would silently skip sentry
    issues for workspace-only projects until the next workspace edit)."""
    store: JobStore = app.state.store
    setup_done = "setup_done" in store.config_all()
    store.config_clear()
    if setup_done:  # onboarding state is not a context override — a reset must
        store.config_set("setup_done", "1")  # not resurrect the first-run wizard
    # memory_canonical_project included: a cleared legacy override must not
    # linger live until restart (finding 1595794/1). repo_map is deliberately
    # NOT reset — workspaces own it; the sync below rebuilds the merged map.
    for key in ("product_name", "business_context", "memory_canonical_project"):
        setattr(settings, key, getattr(_default_settings, key))
    _ws_svc().sync_settings()
    return _context_payload(warning=_live_unmapped_warning(app.state.store))


class SubmitBody(BaseModel):
    project: str
    clickup: str | None = None  # ClickUp task URL or id — adopt an existing ticket
    title: str | None = None    # ... or create a new ticket from title + summary
    summary: str | None = None
    owner: str | None = None       # DEPRECATED alias for dev_dri (applied when dev_dri empty)
    founder_dri: str | None = None  # features: ClickUp user id or username (Epic A2)
    dev_dri: str | None = None      # features: same encoding
    related_to: str | None = None  # features: sibling pipeline job id(s), comma-separated
    gate_mode: str | None = None   # features: 'full' (default) | 'light' (checkpoints + guards)
    success_metric: str | None = None    # features (Epic B1): what should move
    metric_target: str | None = None     # features: target value (free text; numeric when possible)
    metric_window_days: int | None = None  # features: 1-365; empty = instance default at spawn


async def _prepare_ticket(worker: Worker, body: SubmitBody, prefix: str,
                          intro: str) -> dict:
    """Adopt a pasted ClickUp ticket or create one; shared by tasks + features."""
    project = body.project.strip()
    if settings.repo_for_project(project) is None:
        raise HTTPException(status_code=400, detail=f"unknown project '{project}'")

    ref = (body.clickup or "").strip()
    if ref:
        if m := CLICKUP_URL_RE.search(ref):
            cu_id = m.group(1)
        elif CLICKUP_ID_RE.match(ref):
            cu_id = ref
        else:
            raise HTTPException(status_code=400, detail=f"could not parse a ClickUp task from '{ref}'")
        cu_task = await worker.clickup.get_task(cu_id)
        if cu_task is None or cu_task.get("missing"):
            raise HTTPException(
                status_code=404,
                detail=f"ClickUp task '{cu_id}' not found (or ClickUp integration disabled)",
            )
        return {
            "project": project,
            "job_id": f"{prefix}-{cu_task['id']}",
            "title": cu_task["name"] or "untitled",
            "request": cu_task["description"] or cu_task["name"],
            "task_id": cu_task["id"],
            "task_url": cu_task["url"],
            "list_id": cu_task.get("list_id") or "",
            "adopted": True,
        }

    title = (body.title or "").strip()
    summary = (body.summary or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="provide a ClickUp URL, or a title")
    request_text = summary or title
    cu_on, cu_list = _ws_svc().clickup_route(project)
    created = None if not cu_on else await worker.clickup.create_task(
        list_id=cu_list,
        name=f"[{project}] {title}",
        description=(
            f"{intro}\n**Project:** {project}\n\n{request_text}\n\n"
            "_Reply `/proceed <guidance>`, `/redo <notes>` or `/skip` to gate comments, "
            "or answer on the dashboard._"
        ),
    )
    if created:
        task_id, task_url = created
        job_id = f"{prefix}-{task_id}"
        list_id = cu_list or settings.clickup_list_id
    else:  # ClickUp outage degrades tracking, never fixing
        task_id, task_url, list_id = None, None, ""
        job_id = f"{prefix}-{uuid.uuid4().hex[:10]}"
    return {
        "project": project, "job_id": job_id, "title": title, "request": request_text,
        "task_id": task_id, "task_url": task_url, "list_id": list_id or "", "adopted": False,
    }


@app.post("/api/tasks")
async def submit_task(body: SubmitBody, user: dict = Depends(require_user)):
    """Manually reported request (bug fix / change request) — 2-phase HITL flow."""
    worker: Worker = app.state.worker
    if user["role"] != "admin":
        _require_project_access(body.project.strip(), user)
    t = await _prepare_ticket(worker, body, "task", f"**Manual request via the {ENGINE_NAME} dashboard**")
    decision = worker.intake_task(
        t["job_id"], title=t["title"], project=t["project"], request=t["request"],
        clickup_task_id=t["task_id"], clickup_task_url=t["task_url"],
    )
    if "queued" not in decision:
        raise HTTPException(status_code=409, detail=decision)
    if t["adopted"]:
        await worker.clickup.comment(
            t["task_id"] or "",
            f"{ENGINE_NAME} picked this up. I'll analyse the code first and post my plan + "
            "questions here — reply `/proceed <guidance>` or `/skip`, or answer on the dashboard.",
        )
    return {"job_id": t["job_id"], "title": t["title"], "project": t["project"],
            "decision": decision, "clickup_task_url": t["task_url"]}


@app.post("/api/features")
async def submit_feature(body: SubmitBody, user: dict = Depends(require_user)):
    """Feature pipeline: P0-P9 with a human gate after every stage (docs/ENGINE.md)."""
    worker: Worker = app.state.worker
    if user["role"] != "admin":
        _require_project_access(body.project.strip(), user)
    # Epic B1: validate BEFORE any side effect (ticket creation/adoption) —
    # a bad window is a 400 with nothing queued and nothing created
    if body.metric_window_days is not None and not 1 <= body.metric_window_days <= 365:
        raise HTTPException(status_code=400,
                            detail="metric_window_days must be between 1 and 365")
    t = await _prepare_ticket(worker, body, "feat", f"**Feature pipeline via {ENGINE_NAME}**")
    decision = worker.intake_feature(
        t["job_id"], title=t["title"], project=t["project"], request=t["request"],
        clickup_task_id=t["task_id"], clickup_task_url=t["task_url"],
        cu_list_id=t["list_id"],
        founder_dri=(body.founder_dri or "").strip(),
        # legacy `owner` = deprecated alias for dev_dri (applied only when
        # dev_dri is empty); the stored owner column stays a computed alias
        dev_dri=(body.dev_dri or "").strip() or (body.owner or "").strip(),
        related_jobs=(body.related_to or "").strip(),
        gate_mode=(body.gate_mode or "").strip().lower(),
        success_metric=(body.success_metric or "").strip(),
        metric_target=(body.metric_target or "").strip(),
        metric_window_days=body.metric_window_days,
    )
    if "queued" not in decision:
        raise HTTPException(status_code=409, detail=decision)
    if t["adopted"]:
        await worker.clickup.comment(
            t["task_id"] or "",
            f"{ENGINE_NAME} adopted this ticket as a FEATURE PIPELINE (P0 Intake → P9 Ship). "
            "Each stage posts its artifact as a subtask you can edit directly; every stage "
            "parks here for your `/proceed`, `/redo` or `/skip` (or answer on the dashboard).",
        )
    return {"job_id": t["job_id"], "title": t["title"], "project": t["project"],
            "decision": decision, "clickup_task_url": t["task_url"]}


class AnswerBody(BaseModel):
    action: str  # proceed | redo | skip
    answer: str = ""
    override: bool = False  # Epic A3: explicit, audited admin bypass (dashboard-only)


@app.post("/api/jobs/{job_id}/answer")
async def answer_job(job_id: str, body: AnswerBody, user: dict = Depends(require_user)):
    """Answer a parked gate from the dashboard. The decision is recorded on the
    ClickUp ticket; a lost race against a ClickUp comment answer returns 409;
    a role-exclusive gate answered by a non-owner returns 403 (Epic A3) unless
    an admin explicitly sets override (audited in gate_events)."""
    action = body.action.strip().lower()
    if action not in ("proceed", "redo", "skip"):
        raise HTTPException(status_code=400, detail="action must be 'proceed', 'redo' or 'skip'")
    _job_scoped(job_id, user)
    worker: Worker = app.state.worker
    try:
        status = await worker.answer_job(job_id, action, body.answer.strip(),
                                         via=f"dashboard:{user['username']}",
                                         actor=user,
                                         override=body.override and user["role"] == "admin")
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown job '{job_id}'")
    except GateForbidden as e:  # BEFORE ValueError: 403, never a masked 409
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except GateConflict:
        raise HTTPException(status_code=409, detail="already answered via ClickUp")
    return {"job_id": job_id, "status": status}


@app.get("/api/features/{job_id}/stats")
async def feature_stats(job_id: str, user: dict = Depends(require_user)):
    """Per-stage telemetry — the receipts behind the 10x claim. Chat cost rides
    gate_chat rows, never stage_runs (attempt/redo receipts stay clean)."""
    _job_scoped(job_id, user)  # existence + access in one lookup
    store: JobStore = app.state.store
    return {
        "runs": store.stage_runs_for(job_id),
        "guidance": store.guidance_for(job_id),
        "artifacts": store.artifacts_for(job_id),
        "chat": store.chat_for(job_id),
    }


class ChatBody(BaseModel):
    message: str


_chat_tasks: set = set()
chat_broker = ChatBroker()


def _chat_pending(store: JobStore, job_id: str, stage: int, timeout: float,
                  attempt: int | None = None) -> bool:
    # scoped to the current attempt: an unanswered question from before a redo
    # must not wedge the fresh gate's chat for the whole stale-pending window
    last = store.chat_last(job_id, stage, attempt)
    return bool(last and last["role"] == "human"
                and time.time() - last["at"] < timeout + 60)


async def _chat_answer(job: dict, message: str):
    """Background turn: the engine streams progress into the broker; finish()
    fires no matter how the turn ends so no SSE subscriber ever hangs."""
    job_id = job["issue_id"]
    try:
        await app.state.worker.engine.chat(
            job, message,
            publish=lambda event, data: chat_broker.publish(job_id, event, data),
        )
    finally:
        chat_broker.finish(job_id)


@app.post("/api/jobs/{job_id}/chat")
async def gate_chat_post(job_id: str, body: ChatBody, user: dict = Depends(require_user)):
    """Ask the engine a question at a parked gate (docs/CONVERSATIONS.md §2).
    Persist-then-poll: the message is stored and 202-acknowledged immediately;
    a background task answers when the repo workspace frees, and the dashboard
    picks the reply up via GET — nothing is lost if the client disconnects."""
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="empty message")
    store: JobStore = app.state.store
    worker: Worker = app.state.worker
    job = _job_scoped(job_id, user)
    if (job.get("kind") or "") not in ("feature", "sentry", "task"):
        raise HTTPException(status_code=409, detail="chat is not available for this item kind")
    # chat spans the item's whole life: parked at a gate, mid-run (the inbox
    # conversation), AND after it lands — post-mortem questions ("why was this
    # skipped?") answer from the record, whose detail column carries the
    # grading/run outcome. The fast lane never touches the repo; a code-reading
    # escalation queues on the chat clone's lock and lands via persist-then-poll.
    stage = int(job.get("stage") or 0)
    attempt = max(1, int(job.get("stage_attempts") or 1))
    # the turn budget is per GATE: count only this attempt's turns, so a redo
    # (new attempt, new gate) starts with a fresh allotment
    if store.chat_count(job_id, stage, attempt) >= settings.chat_max_turns_per_gate:
        raise HTTPException(status_code=409,
                            detail="chat limit reached for this gate — answer with proceed/redo/skip")
    if _chat_pending(store, job_id, stage, settings.chat_timeout_seconds, attempt):
        raise HTTPException(status_code=409, detail="an answer is already in flight — wait for it")

    store.chat_add(job_id, stage, attempt, "human", message, author=user["username"])
    chat_broker.start(job_id)  # new turn: reset the stream buffer
    task = asyncio.create_task(_chat_answer(job, message))
    _chat_tasks.add(task)  # keep a strong ref — bare create_task results can be GC'd mid-flight
    task.add_done_callback(_chat_tasks.discard)
    return JSONResponse(status_code=202, content={"job_id": job_id, "status": "pending"})


@app.get("/api/jobs/{job_id}/chat")
async def gate_chat_get(job_id: str, user: dict = Depends(require_user)):
    store: JobStore = app.state.store
    job = _job_scoped(job_id, user)
    stage = int(job.get("stage") or 0)
    attempt = max(1, int(job.get("stage_attempts") or 1))
    turns = store.chat_for(job_id, stage)
    pending = _chat_pending(store, job_id, stage, settings.chat_timeout_seconds, attempt)
    if turns and pending:
        turns[-1]["pending"] = True
    return {
        "turns": turns,
        "pending": pending,
        "limit_reached": store.chat_count(job_id, stage, attempt)
                         >= settings.chat_max_turns_per_gate,
    }


@app.get("/api/jobs/{job_id}/chat/stream")
async def gate_chat_stream(job_id: str, user: dict = Depends(require_user)):
    """SSE stream of the in-flight chat turn (docs/CONVERSATIONS.md §5):
    'delta' (answer text chunks), 'status' (progress lines), 'done'. A late or
    reconnecting subscriber replays the buffered turn first. Pure UX on top of
    persist-then-poll — if this stream dies, the polling GET still delivers."""
    _job_scoped(job_id, user)

    async def _events():
        max_s = settings.chat_timeout_seconds + settings.chat_fast_timeout_seconds + 120
        async for event, data in chat_broker.subscribe(job_id, max_seconds=max_s):
            if event == "ping":
                yield ": ping\n\n"
            else:
                yield f"event: {event}\ndata: {json.dumps({'t': data})}\n\n"

    return StreamingResponse(_events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------- live session page (drop in, observe, steer mid-run) ----------

class SteerBody(BaseModel):
    note: str


SESSION_ARTIFACT_MAX = 24000  # per-artifact body cap in the session snapshot payload


@app.get("/api/jobs/{job_id}/session")
async def session_snapshot(job_id: str, user: dict = Depends(require_user)):
    """Everything the detail pane shows for one item: meta, the stage timeline
    (features), gate decisions, the current branch artifacts, the conversation,
    and whether a run is live / steerable right now. All kinds — sentry/task
    items just have empty runs/artifacts and chat primed from their record.
    Read-only; the live activity comes over the SSE stream."""
    store: JobStore = app.state.store
    worker: Worker = app.state.worker
    job = _job_scoped(job_id, user)
    kind = job.get("kind") or "sentry"
    feature = kind == "feature"
    stage = int(job.get("stage") or 0)
    names = [a["artifact"] for a in store.artifacts_for(job_id)] if feature else []
    bodies = store.artifact_contents(job_id, names) if names else {}
    artifacts = [{"name": n, "content": (bodies.get(n) or "")[:SESSION_ARTIFACT_MAX],
                  "truncated": len(bodies.get(n) or "") > SESSION_ARTIFACT_MAX}
                 for n in names]
    live = job["status"] == "running" or worker.engine.stage_broker.active(job_id)
    # graduated autonomy (Epic C3): the per-stage trust dial for this job's
    # workspace+project — pins + earned levels across stages 0–9
    autonomy_block = None
    if feature:
        ws_id = job.get("workspace_id")
        levels = {}
        pins = {}
        if ws_id is not None:
            pins = {str(s): p["pin"]
                    for s, p in store.autonomy_pins_for(int(ws_id)).items()}
            for r in store.autonomy_scores_for(int(ws_id)):
                if r["project"] == (job.get("project") or ""):
                    levels[str(r["stage"])] = {
                        "level": r["level"], "score": r["score"],
                        "sample_runs": r["sample_runs"],
                        "clawed_back": bool(r.get("clawback_at")) and r["level"] == 0,
                    }
        autonomy_block = {"enabled": settings.autonomy_enabled,
                         "auto_level": autonomy.effective_auto_level(settings),
                         "pins": pins, "levels": levels}
    # gate ownership (Epic A3): computed server-side with the acting user so
    # the UI can disable the answer buttons for non-owners (server enforces)
    g_owner = roles.gate_owner(store, settings, _ws_svc().for_job(job), job)
    gate_owner = None
    if g_owner is not None:
        gate_owner = {"role": g_owner.role, "display": g_owner.display,
                      "clickup_id": g_owner.clickup_id, "enforce": g_owner.enforce,
                      "is_you": roles.actor_is_owner(g_owner, user)}
    return {
        "job": {
            "issue_id": job_id, "title": job.get("title") or job_id,
            "kind": kind, "status": job["status"], "project": job.get("project") or "",
            "stage": stage, "stage_name": stage_name(stage) if feature else "",
            "phase": job.get("phase"), "score": job.get("score"),
            "gate_mode": job.get("gate_mode") or "full", "owner": job.get("owner") or "",
            "founder_dri": job.get("founder_dri") or "",
            "dev_dri": job.get("dev_dri") or "",
            "pr_url": job.get("pr_url") or "", "clickup_task_url": job.get("clickup_task_url") or "",
            "issue_url": job.get("issue_url") or "",
            "question": job.get("question") or "", "gate_kind": job.get("gate_kind") or "",
            "evidence": job.get("evidence") or "", "analysis": job.get("analysis") or "",
            "detail": (job.get("detail") or "")[:2000],
            "updated_at": job.get("updated_at"),
            # outcome loop (Epic B): the metric goal + watch window on the pane
            "success_metric": job.get("success_metric") or "",
            "metric_target": job.get("metric_target") or "",
            "metric_window_days": job.get("metric_window_days"),
            "metric_event": job.get("metric_event") or "",
            "watch_deadline": job.get("watch_deadline"),
        },
        "runs": store.stage_runs_for(job_id) if feature else [],
        # recorded run transcripts (§13): the index the Activity accordion is
        # built from — bodies are fetched per run on expand
        "transcripts": transcripts.list_for_job(settings, job_id),
        "guidance": store.guidance_for(job_id),
        # graduated autonomy (Epic C3): pins + per-stage levels (features only)
        "autonomy": autonomy_block,
        # Epic A: gate ownership + the refusal/override/escalation timeline
        "gate_owner": gate_owner,
        "gate_events": store.gate_events_for(job_id),
        "artifacts": artifacts,
        # every PR this packet opened, with lifecycle state (draft -> ready ->
        # in_review -> changes_requested -> approved -> merged/closed)
        "prs": store.prs_for(job_id),
        # the full conversation across ALL stages (the inbox thread), plus the
        # current-stage pending/limit flags the composer needs
        "chat": store.chat_for(job_id),
        "chat_pending": _chat_pending(store, job_id, stage, settings.chat_timeout_seconds,
                                      max(1, int(job.get("stage_attempts") or 1))),
        "chat_limit": store.chat_count(job_id, stage, max(1, int(job.get("stage_attempts") or 1)))
                      >= settings.chat_max_turns_per_gate,
        "chat_available": kind in ("feature", "sentry", "task"),
        # outcome loop (Epic B5): the verdict card — the watch's own ledger row
        # + readings, or the feature's outcome once its watch finished
        "outcome": (store.outcome_for(job_id) if kind == "watch"
                    else (store.outcome_for_feature(job_id) if feature else None)),
        "readings": store.readings_for(job_id) if kind == "watch" else [],
        "live": live,
        # steering rides the feature stage-resume machinery — feature-only
        "steer_available": feature and job["status"] == "running",
        "steer_immediate": bool(settings.session_persistence),
    }


@app.get("/api/jobs/{job_id}/transcripts")
async def job_transcripts(job_id: str, user: dict = Depends(require_user)):
    """The job's recorded run transcripts (docs/ENGINE.md §13): key + header +
    size per run, oldest first — the index behind the Activity accordion."""
    _job_scoped(job_id, user)
    return {"transcripts": transcripts.list_for_job(settings, job_id)}


@app.get("/api/jobs/{job_id}/transcripts/{key}")
async def job_transcript(job_id: str, key: str, user: dict = Depends(require_user)):
    """One run's replayable activity — the same status/delta events the live
    session stream showed, read back from the write-through JSONL."""
    _job_scoped(job_id, user)
    events = transcripts.read_events(settings, job_id, key)
    if events is None:
        raise HTTPException(status_code=404, detail=f"no transcript '{key}'")
    return {"key": key, "events": events}


@app.get("/api/jobs/{job_id}/session/stream")
async def session_stream(job_id: str, user: dict = Depends(require_user)):
    """SSE of the running stage's live activity — 'status' (tool calls / progress),
    'delta' (assistant text), 'done'. Reconnecting subscribers replay the buffered
    run. Keyed on its own broker so a gate chat never clobbers the stage stream."""
    _job_scoped(job_id, user)
    broker = app.state.worker.engine.stage_broker

    async def _events():
        max_s = settings.claude_timeout_seconds + 120
        async for event, data in broker.subscribe(job_id, max_seconds=max_s):
            if event == "ping":
                yield ": ping\n\n"
            else:
                yield f"event: {event}\ndata: {json.dumps({'t': data})}\n\n"

    return StreamingResponse(_events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/jobs/{job_id}/session/steer")
async def session_steer(job_id: str, body: SteerBody, user: dict = Depends(require_user)):
    """Course-correct a running stage. Interrupts the CLI and resumes its session
    with the note folded in when possible; otherwise records it as guidance for
    the next checkpoint. 202 either way with which path was taken."""
    _job_scoped(job_id, user)
    note = body.note.strip()
    if not note:
        raise HTTPException(status_code=400, detail="empty note")
    worker: Worker = app.state.worker
    try:
        outcome = worker.request_steer(job_id, note, via=f"dashboard:{user['username']}")
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown job '{job_id}'")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return JSONResponse(status_code=202, content={"job_id": job_id, "status": outcome})


@app.get("/api/memory")
async def memory_index(user: dict = Depends(require_user)):
    reader = MemoryReader(settings)
    svc = _ws_svc()
    out = {}
    for ws in svc.user_workspaces(user):
        for slug in sorted(svc.public(ws)["repos"]):
            cached = reader.cached(slug)  # one disk read per project, not two
            out[slug] = cached.get("meta", {}) | {"exists": cached["exists"]}
    return out


@app.get("/api/memory/{project}")
async def memory_project(project: str, user: dict = Depends(require_user)):
    ws = _ws_svc().for_project(project)
    if ws is None or not _ws_svc().user_can_access(user, ws["id"]):
        raise HTTPException(status_code=404, detail=f"unknown project '{project}'")
    return MemoryReader(settings).cached(project)


@app.post("/api/memory/{project}/bootstrap")
async def memory_bootstrap(project: str, user: dict = Depends(require_user)):
    ws = _ws_svc().for_project(project)
    if ws is None or not _ws_svc().user_can_access(user, ws["id"]):
        raise HTTPException(status_code=404, detail=f"unknown project '{project}'")
    decision = app.state.worker.intake_memory(project)
    if "queued" not in decision:
        raise HTTPException(status_code=409, detail=decision)
    return {"project": project, "decision": decision}


@app.post("/webhooks/sentry")
async def sentry_webhook(
    request: Request,
    sentry_hook_resource: str = Header(default=""),
    sentry_hook_signature: str = Header(default=""),
):
    body = await request.body()
    if not verify_signature(body, sentry_hook_signature, settings.sentry_client_secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    if sentry_hook_resource == "installation":
        return {"ok": True}  # install/uninstall pings

    payload = json.loads(body)
    ref = extract_issue_ref(sentry_hook_resource, payload)
    if ref is None:
        return {"ok": True, "decision": f"ignored resource '{sentry_hook_resource}'"}

    issue_id, action = ref
    if sentry_hook_resource == "issue" and (
        not settings.handle_new_issues or action != "created"
    ):
        return {"ok": True, "decision": f"ignored issue action '{action}'"}

    data = payload.get("data", {})
    title = (
        data.get("event", {}).get("title")
        or data.get("issue", {}).get("title")
        or "unknown"
    )

    decision = app.state.worker.intake(issue_id, source="webhook", title=title)
    log.info("webhook %s/%s: %s", sentry_hook_resource, action, decision)
    return {"ok": True, "decision": decision}

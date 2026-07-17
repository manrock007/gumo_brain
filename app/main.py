import asyncio
import html
import json
import logging
import re
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
from .config import ENGINE_NAME, RUNTIME_CONTEXT_KEYS, Settings, get_settings
from .db import JobStore
from .feature_prompts import stage_name
from .fixer import ensure_session_store
from .memory import MemoryReader
from .sentry_api import extract_issue_ref, verify_signature
from .worker import GateConflict, Worker
from .workspaces import WorkspaceError, WorkspaceService

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
    worker = Worker(settings, store)
    worker.workspaces = workspaces
    worker.engine.workspaces = workspaces
    worker.engine.memory.canonical_resolver = workspaces.canonical_for
    tasks = [
        asyncio.create_task(worker.run_forever()),
        asyncio.create_task(worker.poll_clickup_forever()),
        asyncio.create_task(worker.sweep_forever()),
        asyncio.create_task(worker.reap_forever()),
        asyncio.create_task(worker.prune_sessions_forever()),
        asyncio.create_task(worker.shepherd_forever()),
    ]
    app.state.store = store
    app.state.worker = worker
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
    # brand subtitle follows the configured product name (docs/ENGINE.md §10)
    return _INDEX_HTML.replace(
        "the Gumo Engine", f"the {html.escape(settings.product_name)} Engine", 1
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


def _public_user(u: dict) -> dict:
    return {"username": u["username"], "role": u["role"],
            "disabled": bool(u.get("disabled")),
            "must_change_pw": bool(u.get("must_change_pw"))}


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
async def change_password(body: PasswordBody, user: dict = Depends(require_user)):
    store: JobStore = app.state.store
    verify_login(store, settings, user["username"], body.current)  # re-authenticate
    if len(body.new) < 8:
        raise HTTPException(status_code=400, detail="new password must be at least 8 characters")
    store.user_set(user["username"], pw_hash=hash_password(body.new), must_change_pw=0)
    store.auth_sessions_revoke_user(user["id"])  # sign out everywhere, incl. this session
    return {"ok": True, "detail": "password changed — sign in again"}


@app.get("/api/users", dependencies=[Depends(require_admin)])
async def users_list():
    return app.state.store.user_list()


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
        if len(body.password) < 8:
            raise HTTPException(status_code=400, detail="password must be at least 8 characters")
        store.user_set(username, pw_hash=hash_password(body.password), must_change_pw=1,
                       failed_attempts=0, locked_until=None)
        store.auth_sessions_revoke_user(user["id"])
    return _public_user(store.user_get(username))


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


@app.post("/api/workspaces", dependencies=[Depends(require_admin)])
async def workspaces_create(body: WorkspaceCreateBody):
    svc = _ws_svc()
    try:
        ws = svc.create(body.slug, body.name,
                        **body.model_dump(exclude={"slug", "name"}, exclude_none=True))
    except WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return svc.public(ws)


@app.patch("/api/workspaces/{workspace_id}", dependencies=[Depends(require_admin)])
async def workspaces_patch(workspace_id: int, body: WorkspacePatchBody):
    svc = _ws_svc()
    try:
        ws = svc.update(workspace_id, repos=body.repos,
                        **body.model_dump(exclude={"slug", "repos"}, exclude_none=True))
    except WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e))
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


@app.get("/api/jobs")
async def jobs(user: dict = Depends(require_user)):
    rows = app.state.store.recent()
    svc = _ws_svc()
    rows = [r for r in rows if svc.user_can_access(user, r.get("workspace_id"))]
    for r in rows:
        if r.get("kind") == "feature":
            r["stage_name"] = stage_name(int(r.get("stage") or 0))
    return rows


class TriggerBody(BaseModel):
    issue: str


@app.post("/api/trigger")
async def trigger(body: TriggerBody, user: dict = Depends(require_user)):
    """Manual fix trigger: Sentry issue id / short id / URL in, ClickUp ticket out."""
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
# context. Env/code defaults (the Gumo repos) apply until an operator saves
# overrides here; overrides persist in the DB and survive restarts.


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
    return {
        "context": settings.project_context(),
        "defaults": _default_settings.project_context(),
        "overridden": sorted(app.state.store.config_all().keys()),
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
    """Drop every stored override — revert to the env/code defaults."""
    app.state.store.config_clear()
    for key in RUNTIME_CONTEXT_KEYS:
        setattr(settings, key, getattr(_default_settings, key))
    return _context_payload(warning=_live_unmapped_warning(app.state.store))


class SubmitBody(BaseModel):
    project: str
    clickup: str | None = None  # ClickUp task URL or id — adopt an existing ticket
    title: str | None = None    # ... or create a new ticket from title + summary
    summary: str | None = None
    owner: str | None = None       # features: ClickUp user id for gate notifications
    related_to: str | None = None  # features: sibling pipeline job id(s), comma-separated
    gate_mode: str | None = None   # features: 'full' (default) | 'light' (checkpoints + guards)


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
    t = await _prepare_ticket(worker, body, "feat", f"**Feature pipeline via {ENGINE_NAME}**")
    decision = worker.intake_feature(
        t["job_id"], title=t["title"], project=t["project"], request=t["request"],
        clickup_task_id=t["task_id"], clickup_task_url=t["task_url"],
        cu_list_id=t["list_id"], owner=(body.owner or "").strip(),
        related_jobs=(body.related_to or "").strip(),
        gate_mode=(body.gate_mode or "").strip().lower(),
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


@app.post("/api/jobs/{job_id}/answer")
async def answer_job(job_id: str, body: AnswerBody, user: dict = Depends(require_user)):
    """Answer a parked gate from the dashboard. The decision is recorded on the
    ClickUp ticket; a lost race against a ClickUp comment answer returns 409."""
    action = body.action.strip().lower()
    if action not in ("proceed", "redo", "skip"):
        raise HTTPException(status_code=400, detail="action must be 'proceed', 'redo' or 'skip'")
    _job_scoped(job_id, user)
    worker: Worker = app.state.worker
    try:
        status = await worker.answer_job(job_id, action, body.answer.strip(),
                                         via=f"dashboard:{user['username']}")
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown job '{job_id}'")
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
    return {
        "job": {
            "issue_id": job_id, "title": job.get("title") or job_id,
            "kind": kind, "status": job["status"], "project": job.get("project") or "",
            "stage": stage, "stage_name": stage_name(stage) if feature else "",
            "phase": job.get("phase"), "score": job.get("score"),
            "gate_mode": job.get("gate_mode") or "full", "owner": job.get("owner") or "",
            "pr_url": job.get("pr_url") or "", "clickup_task_url": job.get("clickup_task_url") or "",
            "issue_url": job.get("issue_url") or "",
            "question": job.get("question") or "", "gate_kind": job.get("gate_kind") or "",
            "evidence": job.get("evidence") or "", "analysis": job.get("analysis") or "",
            "detail": (job.get("detail") or "")[:2000],
            "updated_at": job.get("updated_at"),
        },
        "runs": store.stage_runs_for(job_id) if feature else [],
        "guidance": store.guidance_for(job_id),
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
        "live": live,
        # steering rides the feature stage-resume machinery — feature-only
        "steer_available": feature and job["status"] == "running",
        "steer_immediate": bool(settings.session_persistence),
    }


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
    _job_scoped(job_id, user)
    """Course-correct a running stage. Interrupts the CLI and resumes its session
    with the note folded in when possible; otherwise records it as guidance for
    the next checkpoint. 202 either way with which path was taken."""
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

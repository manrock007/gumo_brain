import asyncio
import json
import logging
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from .config import get_settings
from .dashboard import DASHBOARD_HTML
from .db import JobStore
from .sentry_api import extract_issue_ref, verify_signature
from .worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("brain")

settings = get_settings()
basic = HTTPBasic()

ISSUE_URL_RE = re.compile(r"/issues/(\d+)")
SHORT_ID_RE = re.compile(r"^[A-Z][A-Z0-9_-]*-[A-Z0-9]+$")
# https://app.clickup.com/t/86abcd123 or /t/<team_id>/CUSTOM-123, or a bare task id
CLICKUP_URL_RE = re.compile(r"app\.clickup\.com/t/(?:\d+/)?([A-Za-z0-9_-]+)")
CLICKUP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,}$")


def require_auth(credentials: HTTPBasicCredentials = Depends(basic)):
    if not settings.dashboard_password:
        raise HTTPException(status_code=503, detail="dashboard disabled: DASHBOARD_PASSWORD not set")
    user_ok = secrets.compare_digest(credentials.username.encode(), b"gumo")
    pass_ok = secrets.compare_digest(
        credentials.password.encode(), settings.dashboard_password.encode()
    )
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=401, detail="unauthorized",
                            headers={"WWW-Authenticate": "Basic"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    store = JobStore(settings.db_path)
    worker = Worker(settings, store)
    tasks = [
        asyncio.create_task(worker.run_forever()),
        asyncio.create_task(worker.poll_clickup_forever()),
        asyncio.create_task(worker.sweep_forever()),
    ]
    app.state.store = store
    app.state.worker = worker
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="gumo_brain", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "queued": app.state.worker.queue.qsize()}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def dashboard():
    return DASHBOARD_HTML


@app.get("/api/jobs", dependencies=[Depends(require_auth)])
async def jobs():
    return app.state.store.recent()


class TriggerBody(BaseModel):
    issue: str


@app.post("/api/trigger", dependencies=[Depends(require_auth)])
async def trigger(body: TriggerBody):
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
    decision = worker.intake(issue_id, source="manual", forced=True, title=title, project=project)
    if "queued" not in decision:
        raise HTTPException(status_code=409, detail=decision)

    # create the ticket now so the response includes it (the worker reuses it)
    row = app.state.store.get(issue_id)
    task_url = row.get("clickup_task_url") if row else None
    if row and not row.get("clickup_task_id"):
        created = await worker.clickup.create_task(
            name=f"[{project}] {title}",
            description=worker._ticket_description(issue, row),
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


@app.get("/api/projects", dependencies=[Depends(require_auth)])
async def projects():
    """Configured Sentry-project -> repo mappings, for the dashboard's project picker."""
    mapping = json.loads(settings.repo_map)
    return [{"slug": slug, "repo": entry.get("repo", "")} for slug, entry in sorted(mapping.items())]


class TaskBody(BaseModel):
    project: str
    clickup: str | None = None  # ClickUp task URL or id — adopt an existing ticket
    title: str | None = None    # ... or create a new ticket from title + summary
    summary: str | None = None


@app.post("/api/tasks", dependencies=[Depends(require_auth)])
async def submit_task(body: TaskBody):
    """Manually reported request (bug fix / change request). The ClickUp ticket —
    adopted from a pasted URL or created from title+summary — is the record of work."""
    worker: Worker = app.state.worker
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
        if cu_task is None:
            raise HTTPException(
                status_code=404,
                detail=f"ClickUp task '{cu_id}' not found (or ClickUp integration disabled)",
            )
        title = cu_task["name"] or "untitled request"
        request_text = cu_task["description"] or title
        job_id = f"task-{cu_task['id']}"
        task_id, task_url = cu_task["id"], cu_task["url"]
        picked_up_note = (
            "gumo_brain picked this up. I'll analyse the code first and post my plan + "
            "questions here — reply `/proceed <guidance>` or `/skip`, or answer on the dashboard."
        )
    else:
        title = (body.title or "").strip()
        summary = (body.summary or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="provide a ClickUp URL, or a title")
        request_text = summary or title
        created = await worker.clickup.create_task(
            name=f"[{project}] {title}",
            description=(
                f"**Manual request via gumo_brain dashboard**\n**Project:** {project}\n\n"
                f"{request_text}\n\n"
                "_Claude analyses first and posts its plan + questions below. Reply "
                "`/proceed <guidance>` or `/skip`, or answer on the dashboard._"
            ),
        )
        if created:
            task_id, task_url = created
            job_id = f"task-{task_id}"
        else:  # ClickUp outage degrades tracking, never fixing
            task_id, task_url = None, None
            job_id = f"task-{uuid.uuid4().hex[:10]}"
        picked_up_note = None

    decision = worker.intake_task(
        job_id, title=title, project=project, request=request_text,
        clickup_task_id=task_id, clickup_task_url=task_url,
    )
    if "queued" not in decision:
        raise HTTPException(status_code=409, detail=decision)
    if ref and picked_up_note:
        await worker.clickup.comment(task_id or "", picked_up_note)

    return {
        "job_id": job_id,
        "title": title,
        "project": project,
        "decision": decision,
        "clickup_task_url": task_url,
    }


class AnswerBody(BaseModel):
    action: str  # proceed | skip
    answer: str = ""


@app.post("/api/jobs/{job_id}/answer", dependencies=[Depends(require_auth)])
async def answer_job(job_id: str, body: AnswerBody):
    """Answer an awaiting_input job from the dashboard. The decision is posted to the
    ClickUp ticket (keeper of record) before the job advances."""
    action = body.action.strip().lower()
    if action not in ("proceed", "skip"):
        raise HTTPException(status_code=400, detail="action must be 'proceed' or 'skip'")
    worker: Worker = app.state.worker
    try:
        status = await worker.resolve_awaiting(job_id, action, body.answer.strip())
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown job '{job_id}'")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"job_id": job_id, "status": status}


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

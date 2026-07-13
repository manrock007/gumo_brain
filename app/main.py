import asyncio
import json
import logging
import re
import secrets
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

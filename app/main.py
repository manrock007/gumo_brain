import asyncio
import json
import logging
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from .config import get_settings
from .dashboard import DASHBOARD_HTML
from .db import JobStore
from .feature_prompts import stage_name
from .fixer import ensure_session_store
from .memory import MemoryReader
from .sentry_api import extract_issue_ref, verify_signature
from .worker import GateConflict, Worker

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
    if settings.session_persistence:
        ensure_session_store(settings)
    store = JobStore(settings.db_path)
    worker = Worker(settings, store)
    tasks = [
        asyncio.create_task(worker.run_forever()),
        asyncio.create_task(worker.poll_clickup_forever()),
        asyncio.create_task(worker.sweep_forever()),
        asyncio.create_task(worker.reap_forever()),
        asyncio.create_task(worker.prune_sessions_forever()),
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
    rows = app.state.store.recent()
    for r in rows:
        if r.get("kind") == "feature":
            r["stage_name"] = stage_name(int(r.get("stage") or 0))
    return rows


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
    created = await worker.clickup.create_task(
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
        list_id = settings.clickup_list_id
    else:  # ClickUp outage degrades tracking, never fixing
        task_id, task_url, list_id = None, None, ""
        job_id = f"{prefix}-{uuid.uuid4().hex[:10]}"
    return {
        "project": project, "job_id": job_id, "title": title, "request": request_text,
        "task_id": task_id, "task_url": task_url, "list_id": list_id or "", "adopted": False,
    }


@app.post("/api/tasks", dependencies=[Depends(require_auth)])
async def submit_task(body: SubmitBody):
    """Manually reported request (bug fix / change request) — 2-phase HITL flow."""
    worker: Worker = app.state.worker
    t = await _prepare_ticket(worker, body, "task", "**Manual request via gumo_brain dashboard**")
    decision = worker.intake_task(
        t["job_id"], title=t["title"], project=t["project"], request=t["request"],
        clickup_task_id=t["task_id"], clickup_task_url=t["task_url"],
    )
    if "queued" not in decision:
        raise HTTPException(status_code=409, detail=decision)
    if t["adopted"]:
        await worker.clickup.comment(
            t["task_id"] or "",
            "gumo_brain picked this up. I'll analyse the code first and post my plan + "
            "questions here — reply `/proceed <guidance>` or `/skip`, or answer on the dashboard.",
        )
    return {"job_id": t["job_id"], "title": t["title"], "project": t["project"],
            "decision": decision, "clickup_task_url": t["task_url"]}


@app.post("/api/features", dependencies=[Depends(require_auth)])
async def submit_feature(body: SubmitBody):
    """Feature pipeline: P0-P9 with a human gate after every stage (docs/ENGINE.md)."""
    worker: Worker = app.state.worker
    t = await _prepare_ticket(worker, body, "feat", "**Feature pipeline via gumo_brain**")
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
            "gumo_brain adopted this ticket as a FEATURE PIPELINE (P0 Intake → P9 Ship). "
            "Each stage posts its artifact as a subtask you can edit directly; every stage "
            "parks here for your `/proceed`, `/redo` or `/skip` (or answer on the dashboard).",
        )
    return {"job_id": t["job_id"], "title": t["title"], "project": t["project"],
            "decision": decision, "clickup_task_url": t["task_url"]}


class AnswerBody(BaseModel):
    action: str  # proceed | redo | skip
    answer: str = ""


@app.post("/api/jobs/{job_id}/answer", dependencies=[Depends(require_auth)])
async def answer_job(job_id: str, body: AnswerBody):
    """Answer a parked gate from the dashboard. The decision is recorded on the
    ClickUp ticket; a lost race against a ClickUp comment answer returns 409."""
    action = body.action.strip().lower()
    if action not in ("proceed", "redo", "skip"):
        raise HTTPException(status_code=400, detail="action must be 'proceed', 'redo' or 'skip'")
    worker: Worker = app.state.worker
    try:
        status = await worker.answer_job(job_id, action, body.answer.strip(), via="dashboard")
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown job '{job_id}'")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except GateConflict:
        raise HTTPException(status_code=409, detail="already answered via ClickUp")
    return {"job_id": job_id, "status": status}


@app.get("/api/features/{job_id}/stats", dependencies=[Depends(require_auth)])
async def feature_stats(job_id: str):
    """Per-stage telemetry — the receipts behind the 10x claim. Chat cost rides
    gate_chat rows, never stage_runs (attempt/redo receipts stay clean)."""
    store: JobStore = app.state.store
    if store.get(job_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown job '{job_id}'")
    return {
        "runs": store.stage_runs_for(job_id),
        "guidance": store.guidance_for(job_id),
        "artifacts": store.artifacts_for(job_id),
        "chat": store.chat_for(job_id),
    }


class ChatBody(BaseModel):
    message: str


_chat_tasks: set = set()


def _chat_pending(store: JobStore, job_id: str, stage: int, timeout: float) -> bool:
    last = store.chat_last(job_id, stage)
    return bool(last and last["role"] == "human"
                and time.time() - last["at"] < timeout + 60)


@app.post("/api/jobs/{job_id}/chat", dependencies=[Depends(require_auth)])
async def gate_chat_post(job_id: str, body: ChatBody):
    """Ask the engine a question at a parked gate (docs/CONVERSATIONS.md §2).
    Persist-then-poll: the message is stored and 202-acknowledged immediately;
    a background task answers when the repo workspace frees, and the dashboard
    picks the reply up via GET — nothing is lost if the client disconnects."""
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="empty message")
    store: JobStore = app.state.store
    worker: Worker = app.state.worker
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job '{job_id}'")
    if (job.get("kind") or "") != "feature":
        raise HTTPException(status_code=409, detail="chat is available on feature gates only")
    if job["status"] != "awaiting_input":
        raise HTTPException(status_code=409, detail=f"job is '{job['status']}', not parked at a gate")
    stage = int(job.get("stage") or 0)
    if store.chat_count(job_id, stage) >= settings.chat_max_turns_per_gate:
        raise HTTPException(status_code=409,
                            detail="chat limit reached for this gate — answer with proceed/redo/skip")
    if _chat_pending(store, job_id, stage, settings.chat_timeout_seconds):
        raise HTTPException(status_code=409, detail="an answer is already in flight — wait for it")

    attempt = max(1, int(job.get("stage_attempts") or 1))
    store.chat_add(job_id, stage, attempt, "human", message)
    task = asyncio.create_task(worker.engine.chat(job, message))
    _chat_tasks.add(task)  # keep a strong ref — bare create_task results can be GC'd mid-flight
    task.add_done_callback(_chat_tasks.discard)
    return JSONResponse(status_code=202, content={"job_id": job_id, "status": "pending"})


@app.get("/api/jobs/{job_id}/chat", dependencies=[Depends(require_auth)])
async def gate_chat_get(job_id: str):
    store: JobStore = app.state.store
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job '{job_id}'")
    stage = int(job.get("stage") or 0)
    turns = store.chat_for(job_id, stage)
    pending = _chat_pending(store, job_id, stage, settings.chat_timeout_seconds)
    if turns and pending:
        turns[-1]["pending"] = True
    return {
        "turns": turns,
        "pending": pending,
        "limit_reached": store.chat_count(job_id, stage) >= settings.chat_max_turns_per_gate,
    }


@app.get("/api/memory", dependencies=[Depends(require_auth)])
async def memory_index():
    reader = MemoryReader(settings)
    mapping = json.loads(settings.repo_map)
    out = {}
    for slug in sorted(mapping):
        cached = reader.cached(slug)  # one disk read per project, not two
        out[slug] = cached.get("meta", {}) | {"exists": cached["exists"]}
    return out


@app.get("/api/memory/{project}", dependencies=[Depends(require_auth)])
async def memory_project(project: str):
    if settings.repo_for_project(project) is None:
        raise HTTPException(status_code=404, detail=f"unknown project '{project}'")
    return MemoryReader(settings).cached(project)


@app.post("/api/memory/{project}/bootstrap", dependencies=[Depends(require_auth)])
async def memory_bootstrap(project: str):
    if settings.repo_for_project(project) is None:
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

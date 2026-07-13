import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request

from .config import get_settings
from .db import JobStore
from .sentry_api import extract_issue_ref, verify_signature
from .worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("brain")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    store = JobStore(settings.db_path)
    worker = Worker(settings, store)
    task = asyncio.create_task(worker.run_forever())
    app.state.store = store
    app.state.worker = worker
    yield
    task.cancel()


app = FastAPI(title="gumo_brain", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "queued": app.state.worker.queue.qsize()}


@app.get("/jobs")
async def jobs():
    return app.state.store.recent()


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
    project = (
        data.get("issue", {}).get("project", {}).get("slug")
        or ""  # event_alert payloads carry no slug; the worker resolves it via the API
    )

    decision = app.state.worker.try_enqueue(issue_id, project, title)
    log.info("webhook %s/%s: %s", sentry_hook_resource, action, decision)
    return {"ok": True, "decision": decision}

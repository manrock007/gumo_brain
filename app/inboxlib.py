"""Shared gate-summary computation (Epic I2): ONE implementation of
"overdue" consumed by both GET /api/inbox and the standup digest — the
roles.py single-resolver precedent. Deliberately excludes every per-user
field (is_you / mine): the digest has no acting user; main.py layers those
on top. Imports neither worker nor main."""

import time

from . import roles
from .config import Settings
from .db import JobStore
from .feature_prompts import stage_name


def gate_summary(store: JobStore, settings: Settings, ws: dict | None,
                 job: dict, now: float | None = None):
    """(item, owner) for one answerable job. `item` carries the shared,
    user-independent fields; `owner` is the roles.GateOwner (or None)."""
    now = now or time.time()
    stage = int(job.get("stage") or 0)
    feature = (job.get("kind") or "") == "feature"
    gate_posted = store.latest_gate_posted(job["issue_id"], stage)
    sla = ws["gate_sla_hours"] if ws and ws.get("gate_sla_hours") is not None \
        else settings.gate_sla_hours
    due_at = (gate_posted + sla * 3600) if (sla and gate_posted) else None
    overdue = bool(due_at and now > due_at)
    owner = roles.gate_owner(store, settings, ws, job)
    item = {
        "issue_id": job["issue_id"], "title": job.get("title") or job["issue_id"],
        "kind": job.get("kind") or "sentry", "status": job["status"],
        "project": job.get("project") or "", "workspace_id": job.get("workspace_id"),
        "stage": stage, "stage_name": stage_name(stage) if feature else "",
        "question": (job.get("question") or "")[:300],
        "updated_at": job.get("updated_at"),
        "gate_posted_at": gate_posted, "sla_hours": sla or 0,
        "overdue": overdue, "due_at": due_at,
    }
    return item, owner

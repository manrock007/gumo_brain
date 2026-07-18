"""Pure digest builders (Epic I2 standup, I6 planning pack). outcome.py
style: SQLite reads + arithmetic only — no HTTP, no Claude, no side effects.
The routine handlers persist the result as an inbox item and (on a NEW
insert only) send the Slack copy.

Imports neither worker nor main (routine-module import rule)."""

import calendar
import json
import time
from datetime import datetime, timezone

from . import inboxlib, outcome
from .config import Settings
from .db import JobStore


def month_start(now: float) -> float:
    dt = datetime.fromtimestamp(now, timezone.utc)
    return datetime(dt.year, dt.month, 1, tzinfo=timezone.utc).timestamp()


def month_bounds(now: float) -> tuple[float, int, int]:
    """(month start ts, elapsed days ≥1, days in month) — UTC month math for
    spend pacing."""
    dt = datetime.fromtimestamp(now, timezone.utc)
    start = datetime(dt.year, dt.month, 1, tzinfo=timezone.utc).timestamp()
    days_in_month = calendar.monthrange(dt.year, dt.month)[1]
    elapsed_days = max(1, int((now - start) // 86400) + 1)
    return start, elapsed_days, days_in_month


def effective_budget(ws: dict | None, settings: Settings) -> float:
    """Workspace budget_monthly_usd, NULL = inherit the instance value;
    0 anywhere = no budget (inert)."""
    if ws is not None and ws.get("budget_monthly_usd") is not None:
        return float(ws["budget_monthly_usd"] or 0)
    return float(settings.budget_monthly_usd or 0)


# ---------- I2: the daily standup digest (exceptions only) ----------


def build_standup(store: JobStore, settings: Settings, ws: dict,
                  since: float, now: float | None = None) -> dict | None:
    """The exception-only morning digest for one workspace, or None when
    every section is empty (a quiet day sends NOTHING). `since` = the last
    ok/quiet standup run, floored at now-24h by the caller (a first run or a
    long outage must never replay history)."""
    now = now or time.time()
    ws_id = ws["id"]
    sections: dict[str, list[str]] = {}

    # 1 — gates overdue / breaching SLA (shared "overdue" implementation)
    overdue_lines = []
    for job in store.awaiting_gates():
        if job.get("workspace_id") != ws_id:
            continue
        item, owner = inboxlib.gate_summary(store, settings, ws, job, now)
        if not item["overdue"]:
            continue
        waited_h = int((now - item["gate_posted_at"]) // 3600) \
            if item["gate_posted_at"] else 0
        who = f" — owned by {owner.display}" if owner and owner.enforce else ""
        stage = f"P{item['stage']} " if item["kind"] == "feature" else ""
        overdue_lines.append(f"- {item['title']}: {stage}gate waiting "
                             f"{waited_h}h (SLA {item['sla_hours']}h){who}")
    for e in store.gate_events_by_kind(("sla_standup_flag",), since, ws_id):
        overdue_lines.append(f"- {e.get('job_title') or e['job_id']}: SLA "
                             f"escalation exhausted ({e.get('detail') or ''})")
    if overdue_lines:
        sections["Gates overdue"] = overdue_lines

    # 2 — blocked/stalled pipelines
    blocked_lines = []
    for job in store.by_status(["error", "timeout"]):
        if job.get("workspace_id") != ws_id:
            continue
        blocked_lines.append(f"- {job.get('title') or job['issue_id']}: "
                             f"{job['status']} — {(job.get('detail') or '')[:120]}")
    for pr in store.prs_in_state_with_workspace(("stalled",)):
        if pr.get("workspace_id") != ws_id:
            continue
        blocked_lines.append(f"- PR stalled ({pr.get('job_title') or pr['job_id']}): "
                             f"{pr['url']}")
    if blocked_lines:
        sections["Blocked / stalled"] = blocked_lines

    # 3 — watch jobs trending off-goal mid-window
    watch_lines = []
    for job in store.by_status(["watching"]):
        if (job.get("kind") or "") != "watch" or job.get("workspace_id") != ws_id:
            continue
        readings = store.readings_for(job["issue_id"],
                                      window_start=job.get("watch_started_at"))
        trend = outcome.mid_window_trend(
            readings, job.get("metric_target") or "",
            int(job.get("metric_window_days") or 0),
            settings.outcome_flat_band_pct)
        if trend == "regressing":
            watch_lines.append(f"- {job.get('title') or job['issue_id']}: metric "
                               f"trending to miss '{job.get('metric_target')}'")
    if watch_lines:
        sections["Watches trending off-goal"] = watch_lines

    # 4 — budget/spend position (only when a budget is configured or money
    # was actually spent)
    start, elapsed_days, days_in_month = month_bounds(now)
    spend = store.costs_since(start).get(ws_id, 0.0)
    budget = effective_budget(ws, settings)
    if budget > 0 or spend > 0:
        line = f"- Month-to-date spend: ${spend:.2f}"
        if budget > 0:
            projected = spend / elapsed_days * days_in_month
            line += f" of ${budget:.2f} budget"
            if projected > budget:
                line += (f" — PACING OVER BUDGET (projected month-end "
                         f"${projected:.2f})")
            sections["Budget"] = [line]
        elif spend > 0:
            sections["Budget"] = [line + " (no budget configured)"]

    # 5 — autonomy changes since yesterday
    autonomy_lines = []
    for e in store.autonomy_events_recent([ws_id], 50, since=since):
        if e["kind"] in ("level_change", "pin_set", "pin_clear", "clawback"):
            autonomy_lines.append(f"- {e.get('detail') or e['kind']}"
                                  f" ({e.get('actor') or 'engine'})")
    if autonomy_lines:
        sections["Autonomy changes"] = autonomy_lines

    if not sections:
        return None
    body_parts = []
    for heading, lines in sections.items():
        body_parts.append(f"## {heading}\n" + "\n".join(lines))
    return {
        "title": f"Standup — {ws['name']}",
        "body": "\n\n".join(body_parts),
        "sections": sections,
    }

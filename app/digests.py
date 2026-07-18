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


# ---------- I6: the weekly planning pack ----------


def week_stats(runs: list[dict], redos: list[dict]) -> dict:
    """Receipts for one week of stage runs — the exact column semantics Epic
    C1's scorer established: OPEN runs (result_status='') are excluded from
    every denominator; gate wait uses only runs with BOTH gate timestamps."""
    closed = [r for r in runs if (r.get("result_status") or "")]
    cost = sum(float(r.get("cost_usd") or 0) for r in runs)
    waits = sorted((r["gate_answered_at"] - r["gate_posted_at"]) for r in runs
                   if r.get("gate_posted_at") and r.get("gate_answered_at"))
    median_wait = waits[len(waits) // 2] if waits else None
    answered = [r for r in closed if r.get("gate_answered_at")]
    redo_rate = (len(redos) / len(answered)) if answered else 0.0
    return {"runs": len(closed), "cost_usd": round(cost, 2),
            "median_gate_wait_s": median_wait,
            "redo_rate": round(redo_rate, 3), "answered_gates": len(answered)}


def _trend(cur, prev) -> str:
    if prev in (None, 0) and cur in (None, 0):
        return "→"
    if prev in (None, 0):
        return "↑"
    if cur is None:
        return "→"
    if cur > prev * 1.05:
        return "↑"
    if cur < prev * 0.95:
        return "↓"
    return "→"


RANKING_FORMULA = ("ranking: regressed-outcome sources first, then "
                   "risk-linked (sentry clusters), then by evidence count "
                   "desc, then oldest first")


def rank_proposals(proposals: list[dict]) -> list[dict]:
    """Deterministic 'what I'd do next lap' ordering — a transparent formula
    (stated in the pack body), never a model run."""
    def key(p):
        try:
            refs = json.loads(p.get("refs") or "{}")
            if not isinstance(refs, dict):
                refs = {}
        except (ValueError, TypeError):
            refs = {}
        source_kind = refs.get("source_kind") or ""
        if refs.get("verdict") == "regressed":
            priority = 0
        elif source_kind == "sentry-cluster":
            priority = 1
        else:
            priority = 2
        return (priority, -int(refs.get("count") or 0),
                float(p.get("created_at") or 0), int(p.get("id") or 0))
    return sorted(proposals, key=key)


def build_planning_pack(store: JobStore, settings: Settings, ws: dict,
                        now: float | None = None) -> dict:
    """The weekly review ENGINE.md §8 promised, assembled MECHANICALLY:
    receipts (this week vs last, trend arrows), outcome-ledger movement,
    autonomy shifts, open proposals, and the ranked next-lap list."""
    now = now or time.time()
    ws_id = ws["id"]
    week = 7 * 86400
    this_runs = store.stage_runs_window(ws_id, now - week, now)
    prev_runs = store.stage_runs_window(ws_id, now - 2 * week, now - week)
    this_stats = week_stats(this_runs, store.redo_rows_window(ws_id, now - week, now))
    prev_stats = week_stats(prev_runs,
                            store.redo_rows_window(ws_id, now - 2 * week, now - week))

    def fmt_wait(s):
        return f"{s / 3600:.1f}h" if s is not None else "n/a"

    receipts_lines = [
        f"- Runs: {this_stats['runs']} "
        f"({_trend(this_stats['runs'], prev_stats['runs'])} vs {prev_stats['runs']})",
        f"- Cost: ${this_stats['cost_usd']:.2f} "
        f"({_trend(this_stats['cost_usd'], prev_stats['cost_usd'])} vs "
        f"${prev_stats['cost_usd']:.2f})",
        f"- Median gate wait: {fmt_wait(this_stats['median_gate_wait_s'])} "
        f"({_trend(this_stats['median_gate_wait_s'], prev_stats['median_gate_wait_s'])} "
        f"vs {fmt_wait(prev_stats['median_gate_wait_s'])})",
        f"- Redo rate: {this_stats['redo_rate']:.0%} "
        f"({_trend(this_stats['redo_rate'], prev_stats['redo_rate'])} vs "
        f"{prev_stats['redo_rate']:.0%})",
    ]

    this_outcomes = store.outcomes_decided_window(ws_id, now - week, now)
    prev_outcomes = store.outcomes_decided_window(ws_id, now - 2 * week, now - week)

    def dist(rows):
        d: dict[str, int] = {}
        for r in rows:
            v = r.get("verdict") or "unmeasured"
            d[v] = d.get(v, 0) + 1
        return d

    outcome_lines = [f"- Decided this week: {len(this_outcomes)} "
                     f"({dist(this_outcomes)}) vs last week "
                     f"{len(prev_outcomes)} ({dist(prev_outcomes)})"]
    for r in this_outcomes:
        outcome_lines.append(f"  - {r.get('feature_id')}: {r.get('verdict')}"
                             + (f" — {(r.get('learning') or '')[:120]}"
                                if (r.get("learning") or "").strip() else ""))

    autonomy_lines = []
    for e in store.autonomy_events_recent([ws_id], 100, since=now - week):
        if e["kind"] in ("level_change", "pin_set", "pin_clear", "clawback"):
            autonomy_lines.append(f"- {e.get('detail') or e['kind']}")
    if not autonomy_lines:
        autonomy_lines = ["- no shifts this week"]

    proposals = store.inbox_items_open([ws_id], kinds=("proposal",))
    ranked = rank_proposals(proposals)
    proposal_lines = [f"- #{p['id']}: {p['title']}" for p in ranked] \
        or ["- none open"]
    next_lap = [f"{i + 1}. {p['title']} (notice #{p['id']})"
                for i, p in enumerate(ranked[:5])] \
        or ["1. Nothing queued — a quiet lap."]

    body = "\n\n".join([
        "## Receipts (this week vs last)\n" + "\n".join(receipts_lines),
        "## Outcome-ledger movement\n" + "\n".join(outcome_lines),
        "## Autonomy shifts\n" + "\n".join(autonomy_lines),
        "## Open proposals\n" + "\n".join(proposal_lines),
        f"## What I'd do next lap\n_({RANKING_FORMULA})_\n\n"
        + "\n".join(next_lap),
    ])
    return {"title": f"Weekly planning pack — {ws['name']}", "body": body,
            "receipts": {"this_week": this_stats, "last_week": prev_stats},
            "ranked": [p["id"] for p in ranked]}

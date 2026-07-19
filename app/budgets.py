"""Per-workspace monthly budgets (Epic G4).

Substrate already present: workspaces.budget_monthly_usd, db.costs_since,
signals.spend_alert. This module resolves the effective budget and the
warn/block state, and is the single place the worker/engine consult before a
Claude run is dispatched.

Fail-closed edges: budget 0 (or unset with a 0 instance fallback) is INERT —
state is always 'ok', nothing ever blocks (a missing budget must never
accidentally park work). A resolved budget >= 100% blocks a non-forced run when
block is enabled.
"""

from . import digests


def resolve_budget(settings, workspace: dict | None) -> float:
    """ws.budget_monthly_usd when not NULL, else the instance fallback."""
    if workspace is not None and workspace.get("budget_monthly_usd") is not None:
        return float(workspace["budget_monthly_usd"] or 0)
    return float(getattr(settings, "budget_monthly_usd", 0) or 0)


def budget_status(store, settings, workspace: dict | None, now: float | None = None) -> dict:
    """{budget, spent, pct, state} for one workspace. state:
       'ok'    < warn pct
       'warn'  >= warn pct and < 100%
       'block' >= 100%  (only meaningful when budget_block_enabled)."""
    budget = resolve_budget(settings, workspace)
    ws_id = workspace.get("id") if workspace else None
    month_start = digests.month_start(now) if now else digests.month_start(__import__("time").time())
    spent = float(store.costs_since(month_start).get(ws_id, 0.0)) if ws_id is not None else 0.0
    if budget <= 0:
        return {"budget": 0.0, "spent": round(spent, 4), "pct": 0.0, "state": "ok"}
    pct = spent / budget * 100
    warn_pct = getattr(settings, "budget_warn_pct", 80)
    if pct >= 100:
        state = "block"
    elif pct >= warn_pct:
        state = "warn"
    else:
        state = "ok"
    return {"budget": round(budget, 2), "spent": round(spent, 4),
            "pct": round(pct, 1), "state": state}


def should_block(store, settings, workspace: dict | None, override: bool,
                 now: float | None = None) -> tuple[bool, dict]:
    """(blocked, status). A run is blocked only when block is enabled, the
    workspace is at/over 100%, and the run does NOT carry an explicit budget
    override. Fail closed to NOT blocking when budget is inert (0).

    IMPORTANT: `override` must be a DELIBERATE budget-override signal (an admin
    re-kick), NOT the generic intake `forced` flag — intake stamps forced=1 on
    every human-submitted job, so keying the exemption off `forced` would make
    the block inert for the entire feature pipeline (Epic G4)."""
    status = budget_status(store, settings, workspace, now)
    if override:
        return False, status
    if not getattr(settings, "budget_block_enabled", True):
        return False, status
    return status["state"] == "block", status

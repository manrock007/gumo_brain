"""Graduated autonomy — the trust ladder (Epic C, docs/ENGINE.md §15).

Per (workspace, repo, stage) the engine earns an autonomy LEVEL 0–3 from its
own receipts (`stage_runs` + `guidance_log` + `prs`):

    score = 0.40 * clean_rate            # STAGE_DONE closes / counted runs
          + 0.30 * (1 - redo_rate)       # human redos targeting this stage / answered gates
          + 0.15 * latency_factor        # how fast humans wave this stage through
          + 0.15 * rounds_factor         # shepherd review rounds (code stages only)

    level: score >= 0.90 -> 3, >= 0.75 -> 2, >= 0.55 -> 1, else 0
    overrides: sample < autonomy_min_runs        -> level 0
               level 3 additionally requires clean_streak >= autonomy_min_runs
               AND at least one HUMAN-ANSWERED gate in the window (a cell that
               only ever auto-advances can never hold level 3 on autopilot)

Neutral-empty semantics (fail closed, spelled out):
- open runs (result_status='') plus 'interrupted' / 'skipped_single_group'
  are excluded from every denominator;
- latency uses only runs with BOTH gate_posted_at AND gate_answered_at; an
  empty latency sample is NEUTRAL (latency_factor = 1.0);
- zero answered gates -> redo_rate = 0.0, i.e. the (1 - redo_rate) term gives
  FULL credit — which is why level 3 demands a human-answered run;
- a cell with no runs left in the window decays to level 0 on the next pass;
- clawed-back cells only see runs/redos AFTER clawback_at (re-earn from zero).

Stage 9 never auto-advances (its proceed is the terminal transition owned by
worker._answer_feature); levels cover stages 0–8 only.

The exact numbers each cell's formula saw are persisted in
`autonomy_scores.inputs` (transparency requirement).
"""

import json
import logging
import statistics
import time

log = logging.getLogger("brain.autonomy")

# Light gate mode's auto-advance stages — P0/P1/P3/P9 always park under light
# mode itself (a workspace `always_auto` pin or a computed level >= the
# opt-in autonomy_auto_level can still fire for stages outside this set,
# P9 excepted — docs/ENGINE.md §15). Engine.LIGHT_MODE_AUTO_STAGES aliases
# this constant (the module lives here to avoid an engine<->autonomy cycle).
LIGHT_MODE_AUTO_STAGES = {2, 4, 5, 6, 7, 8}

LEVEL_THRESHOLDS = ((0.90, 3), (0.75, 2), (0.55, 1))
WEIGHTS = {"clean": 0.40, "redo": 0.30, "latency": 0.15, "rounds": 0.15}
CODE_STAGES = {5, 6, 7, 8}
# excluded from every denominator: open rows and non-judgeable closes
SKIP_RESULT_STATUSES = {"", "interrupted", "skipped_single_group"}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _level_for(score: float) -> int:
    for threshold, level in LEVEL_THRESHOLDS:
        if score >= threshold:
            return level
    return 0


def effective_auto_level(settings) -> int:
    """The opt-in computed-level rung, validated. Values outside 1..3 DISABLE
    the computed-level rule (returns 0) — never clamped toward permissiveness:
    0 plausibly means 'never', 4+ means 'unreachable/off'; both must gate."""
    try:
        level = int(getattr(settings, "autonomy_auto_level", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return level if 1 <= level <= 3 else 0


# ---------- the nightly scorer (C1) ----------

def compute(store, settings, now: float | None = None) -> dict:
    """One scoring pass over the rolling window. Recomputes every cell that
    has window runs PLUS every previously-stored cell (so an emptied window
    decays a stale level back to 0 — fail closed). Synchronous by design;
    callers on the event loop wrap it in asyncio.to_thread."""
    now = now or time.time()
    window_days = int(getattr(settings, "autonomy_window_days", 30) or 30)
    min_runs = int(getattr(settings, "autonomy_min_runs", 5) or 5)
    sla_hours = int(getattr(settings, "gate_sla_hours", 24) or 0) or 24
    sla_seconds = sla_hours * 3600
    max_rounds = int(getattr(settings, "pr_max_review_rounds", 6) or 6)
    since = now - window_days * 86400

    run_rows = store.autonomy_run_rows(since)
    redo_rows = store.autonomy_redo_rows(since)
    rounds_by_project = store.shepherd_rounds_by_project(since)
    prior = {(r["workspace_id"], r["project"], r["stage"]): r
             for r in store.autonomy_scores_all()}

    cells: dict[tuple, list[dict]] = {}
    for r in run_rows:
        cells.setdefault((r["workspace_id"], r["project"], r["stage"]), []).append(r)
    redos: dict[tuple, list[dict]] = {}
    for g in redo_rows:
        redos.setdefault((g["workspace_id"], g["project"], g["stage"]), []).append(g)

    changed = 0
    keys = set(cells) | set(prior)
    for key in sorted(keys, key=lambda k: (k[0], k[1], k[2])):
        ws_id, project, stage = key
        clawback_at = (prior.get(key) or {}).get("clawback_at")

        rows = cells.get(key, [])
        if clawback_at:
            rows = [r for r in rows if (r.get("started_at") or 0) > clawback_at]
        runs = [r for r in rows
                if (r.get("result_status") or "") not in SKIP_RESULT_STATUSES]
        sample = len(runs)

        cell_redos = redos.get(key, [])
        if clawback_at:
            cell_redos = [g for g in cell_redos if (g.get("at") or 0) > clawback_at]

        done = sum(1 for r in runs if r["result_status"] == "done")
        clean_rate = done / sample if sample else 0.0

        answered = [r for r in runs if r.get("gate_answered_at")]
        redo_count = len(cell_redos)
        # zero answered gates -> neutral 0 (FULL credit on the redo term);
        # level 3 below therefore demands >=1 human-answered run
        redo_rate = _clamp01(redo_count / len(answered)) if answered else 0.0

        latency_pairs = [r["gate_answered_at"] - r["gate_posted_at"] for r in runs
                         if r.get("gate_posted_at") and r.get("gate_answered_at")]
        if latency_pairs:
            median_latency = statistics.median(latency_pairs)
            latency_factor = _clamp01(1 - median_latency / (2 * sla_seconds))
        else:
            median_latency = None
            latency_factor = 1.0  # neutral: no human-answered gates to time

        avg_rounds = rounds_by_project.get(project)
        if stage in CODE_STAGES and avg_rounds is not None:
            rounds_factor = _clamp01(1 - avg_rounds / max_rounds)
        else:
            rounds_factor = 1.0  # doc stages / no PR data: neutral

        score = (WEIGHTS["clean"] * clean_rate
                 + WEIGHTS["redo"] * (1 - redo_rate)
                 + WEIGHTS["latency"] * latency_factor
                 + WEIGHTS["rounds"] * rounds_factor)

        # clean_streak: consecutive most-recent counted runs that ended 'done'
        # and were not answered with a redo; skipped statuses don't break it
        clean_streak = 0
        for r in reversed(rows):
            status = r.get("result_status") or ""
            if status in SKIP_RESULT_STATUSES:
                continue
            if status == "done" and (r.get("gate_action") or "") != "redo":
                clean_streak += 1
            else:
                break

        level = _level_for(score)
        if sample < min_runs:
            level = 0
        elif level == 3 and (clean_streak < min_runs or not answered):
            # self-reinforcement bound: full trust needs a fresh streak AND at
            # least one human-answered gate inside the window
            level = 2

        inputs = {
            "window_days": window_days, "sample": sample,
            "clean_rate": round(clean_rate, 4), "done": done,
            "redo_rate": round(redo_rate, 4), "redo_count": redo_count,
            "answered_gates": len(answered),
            "latency_factor": round(latency_factor, 4),
            "median_gate_latency_s": (round(median_latency, 1)
                                      if median_latency is not None else None),
            "rounds_factor": round(rounds_factor, 4),
            "avg_review_rounds": (round(avg_rounds, 2)
                                  if avg_rounds is not None else None),
            "clean_streak": clean_streak,
        }
        res = store.autonomy_score_upsert(
            ws_id, project, stage, level, round(score, 4), json.dumps(inputs),
            sample, computed_started=now)
        prev = res["prev_level"] if res["prev_level"] is not None else 0
        if res["applied"] and res["level"] != prev:
            changed += 1
            store.autonomy_event_add(
                kind="level_change", workspace_id=ws_id, project=project,
                stage=stage,
                detail=(f"P{stage} {project}: level {prev} → {res['level']} "
                        f"(score {score:.2f}, {sample} runs)"),
                actor="engine")
    return {"cells": len(keys), "changed": changed}


# ---------- gate resolution (C2) ----------

def resolve_gate(store, settings, job: dict, stage: int) -> tuple[str, str]:
    """('auto'|'gate', reason) for a clean STAGE_DONE at `stage`. Resolution
    order (locked): workspace pin > per-job gate_mode='light' (incl. its
    LIGHT_MODE_AUTO_STAGES restriction) > computed level >= the opt-in
    autonomy_auto_level > default full gating.

    Fail-closed edges: stage 9 always gates (terminal transition, pins
    ignored); a missing workspace_id skips PIN and LEVEL resolution ONLY —
    the legacy workspace-independent light-mode path still applies; with
    autonomy_enabled=False only the light-mode path exists. The safety guards
    (engine._auto_guards_ok) apply unconditionally AFTER this resolver."""
    stage = int(stage)
    if stage >= 9:
        return "gate", "terminal stage"
    light_ok = ((job.get("gate_mode") or "full") == "light"
                and stage in LIGHT_MODE_AUTO_STAGES)
    ws_id = job.get("workspace_id")
    if not getattr(settings, "autonomy_enabled", True) or ws_id is None:
        if light_ok:
            return "auto", "light gate mode"
        return "gate", "full gating"

    pin = (store.autonomy_pins_for(int(ws_id)).get(stage) or {}).get("pin")
    if pin == "always_gate":
        return "gate", "pin: always_gate"
    if pin == "always_auto":
        return "auto", "pin: always_auto"

    if light_ok:
        return "auto", "light gate mode"

    auto_level = effective_auto_level(settings)
    if auto_level:
        row = store.autonomy_score_get(int(ws_id), job.get("project") or "", stage)
        if row and int(row["level"] or 0) >= auto_level:
            streak = None
            try:
                streak = json.loads(row.get("inputs") or "{}").get("clean_streak")
            except (ValueError, TypeError):
                pass
            reason = f"autonomy level {row['level']}"
            if streak:
                reason += f", {streak} clean runs"
            return "auto", reason
    return "gate", "full gating"


# ---------- clawback (C3) ----------

def clawback(store, settings, workspace_id: int, stage: int,
             project: str | None, actor: str) -> int:
    """One-click brake: zero the level(s) and stamp clawback_at so the cell
    re-earns from scratch. project=None claws back every slug that ever held
    a cell in this workspace plus the current repo set (db-side, one
    transaction). Emits one audited event per cell; returns the cell count."""
    affected = store.autonomy_clawback(int(workspace_id), int(stage), project)
    for slug in affected:
        store.autonomy_event_add(
            kind="clawback", workspace_id=int(workspace_id), project=slug,
            stage=int(stage),
            detail=f"P{stage} {slug}: level dropped to 0 — re-earning from zero",
            actor=actor)
    return len(affected)

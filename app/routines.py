"""The routine engine (Epic I1, docs/ENGINE.md §17).

Generalizes the hardcoded background loops into DB-backed, schedulable
routines: a `routines` row per (kind, scope), a single scheduler task driving
them, INSERT-only run history, and every Epic I routine's output landing as a
durable inbox item — never a silent side effect.

Contract (locked):
- A routine NEVER invokes Claude. Handlers are HTTP + SQLite work on the
  event loop; the ONE sanctioned queue interaction is the memory-upkeep
  routine's `worker.intake_memory` (a parked draft-PR job like any manual
  bootstrap). The shepherd (which DOES run Claude) deliberately stays a
  native worker loop, alongside sla/watch/autonomy/poll_clickup.
- Builtin instance rows (sweep, reaper, janitor — workspace_id NULL) store
  schedule='' meaning "derive from live settings at each tick", so env
  contracts (SWEEP_INTERVAL_HOURS, …) keep working; an operator-edited
  non-empty schedule wins. A builtin row whose stored schedule fails to parse
  FALLS BACK to its derived default with a logged error — never a silently
  disabled reaper. The reaper is non-disableable outright.
- ROUTINES_ENABLED=false silences ONLY the per-workspace Epic I routines;
  builtins keep firing ('off' never means 'less safe').
- Fail closed: an unparseable schedule on a WORKSPACE routine disables it
  with last_status='error: bad schedule' — never a guessed cadence.
- Single-flight per routine: a claim CAS on last_run_at (one winner per due
  firing) plus an in-process in-flight guard; each due handler runs as its
  own asyncio task so a slow handler can never block the reaper.

This module imports neither worker nor main at module level (the worker
arrives via the RoutineContext) so WorkspaceService.create can seed new
workspaces without an import cycle.
"""

import asyncio
import json
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import digests, outcome, signals
from .config import Settings
from .db import JobStore

log = logging.getLogger("brain.routines")

BUILTIN_KINDS = ("sweep", "reaper", "janitor")
WORKSPACE_KINDS = ("standup_digest", "memory_upkeep", "risk_scan",
                   "proposal_scan", "weekly_planning")

# statuses that count as "the job is live" without importing worker
ACTIVE_JOB_STATUSES = ("received", "queued", "running", "awaiting_input")

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

EVERY_FLOOR_SECONDS = 300  # never a hot loop

ROUTINE_RUNS_KEEP_LATEST = 20  # per-routine floor the janitor always keeps


@dataclass
class Schedule:
    kind: str                      # 'every' | 'daily' | 'weekly'
    seconds: int = 0               # every
    hh: int = 0
    mm: int = 0
    days: frozenset = frozenset()  # weekday ints (0=mon)


def parse_schedule(spec: str) -> Schedule:
    """Parse 'every:<seconds>' | 'daily@HH:MM[;days=mon,tue,…]' |
    'weekly@<day> HH:MM'. Raises ValueError on anything else — callers decide
    the fail-closed consequence (workspace routines disable; builtins fall
    back to their derived default)."""
    spec = (spec or "").strip()
    if spec.startswith("every:"):
        try:
            seconds = int(spec[len("every:"):])
        except ValueError:
            raise ValueError(f"bad every: seconds in '{spec}'")
        if seconds <= 0:
            raise ValueError(f"every: seconds must be positive in '{spec}'")
        return Schedule("every", seconds=max(EVERY_FLOOR_SECONDS, seconds))
    if spec.startswith("daily@"):
        rest = spec[len("daily@"):]
        parts = rest.split(";")
        hh, mm = _parse_hhmm(parts[0])
        days = frozenset(_DAYS.values())
        for extra in parts[1:]:
            extra = extra.strip()
            if not extra.startswith("days="):
                raise ValueError(f"unknown daily option '{extra}'")
            names = [d.strip().lower() for d in extra[len("days="):].split(",") if d.strip()]
            if not names or any(d not in _DAYS for d in names):
                raise ValueError(f"bad days list in '{spec}'")
            days = frozenset(_DAYS[d] for d in names)
        return Schedule("daily", hh=hh, mm=mm, days=days)
    if spec.startswith("weekly@"):
        rest = spec[len("weekly@"):].strip()
        try:
            day_name, hhmm = rest.split(None, 1)
        except ValueError:
            raise ValueError(f"weekly@ needs '<day> HH:MM' in '{spec}'")
        day = _DAYS.get(day_name.strip().lower())
        if day is None:
            raise ValueError(f"unknown weekday '{day_name}' in '{spec}'")
        hh, mm = _parse_hhmm(hhmm)
        return Schedule("weekly", hh=hh, mm=mm, days=frozenset({day}))
    raise ValueError(f"unrecognized schedule '{spec}'")


def _parse_hhmm(text: str) -> tuple[int, int]:
    try:
        hh_s, mm_s = text.strip().split(":")
        hh, mm = int(hh_s), int(mm_s)
    except ValueError:
        raise ValueError(f"bad HH:MM '{text}'")
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"bad HH:MM '{text}'")
    return hh, mm


def _next_occurrence(sched: Schedule, after: float, tz) -> float:
    """The earliest daily/weekly occurrence strictly after `after`, evaluated
    in tz (zoneinfo — DST transitions resolve through the zone rules)."""
    dt = datetime.fromtimestamp(after, tz)
    for i in range(0, 9):
        day = (dt + timedelta(days=i)).date()
        if day.weekday() not in sched.days:
            continue
        occ = datetime.combine(day, dtime(sched.hh, sched.mm), tzinfo=tz)
        if occ.timestamp() > after:
            return occ.timestamp()
    raise ValueError("no occurrence found (empty days?)")  # unreachable: days validated


def next_due(sched: Schedule, last_run_at: float | None, now: float, tz) -> float:
    """When the routine should next fire. every: rows with no history are due
    immediately; daily/weekly rows with no history wait for their NEXT
    occurrence (a fresh seed must not fire a missed slot at boot).

    NOTE: for daily/weekly rows the anchor MUST be persisted — a NULL
    last_run_at anchors at `now`, which re-slides forward on every tick, so
    the computed due time is always strictly in the future and the row can
    never come due. Seeding therefore stamps last_run_at for daily/weekly
    rows (anchor_fresh_rows); the `now` fallback here is defensive only."""
    if sched.kind == "every":
        if last_run_at is None:
            return now
        return last_run_at + sched.seconds
    anchor = last_run_at if last_run_at is not None else now
    return _next_occurrence(sched, anchor, tz)


def builtin_default_schedule(kind: str, settings: Settings) -> str:
    """Derived-from-live-settings cadence for builtin rows (schedule='')."""
    if kind == "sweep":
        return f"every:{max(EVERY_FLOOR_SECONDS, settings.sweep_interval_hours * 3600)}"
    if kind == "reaper":
        return "every:300"
    if kind == "janitor":
        return "every:86400"
    raise ValueError(f"not a builtin kind: {kind}")


@dataclass
class RoutineContext:
    settings: Settings
    store: JobStore
    worker: object            # app.worker.Worker (never imported here)
    workspaces: object        # WorkspaceService | None
    routine: dict
    config: dict = field(default_factory=dict)
    now: float = 0.0

    @property
    def workspace(self) -> dict | None:
        ws_id = self.routine.get("workspace_id")
        return self.store.workspace_get(int(ws_id)) if ws_id is not None else None


# ---------- builtin handlers (existing loop bodies, unchanged) ----------


async def _handle_sweep(ctx: RoutineContext):
    if not (ctx.settings.sweep_enabled and ctx.settings.sentry_enabled):
        return "skipped", "sweep disabled or Sentry unconfigured", 0
    await ctx.worker._sweep_once()
    return "ok", "", 0


async def _handle_reaper(ctx: RoutineContext):
    await ctx.worker._reap_once(ctx.worker.reap_horizon())
    return "ok", "", 0


async def _handle_janitor(ctx: RoutineContext):
    ctx.worker._janitor_once()
    s = ctx.settings
    expired = ctx.store.inbox_items_expire(
        ("risk_alert", "routine_note", "standup_digest", "planning_pack"),
        s.inbox_notice_ttl_days)
    pruned = ctx.store.routine_runs_prune(s.routine_run_ttl_days,
                                          ROUTINE_RUNS_KEEP_LATEST)
    return "ok", f"expired {expired} notice(s); pruned {pruned} run row(s)", 0


def routine_tz(settings: Settings):
    try:
        return ZoneInfo(settings.routine_tz or "UTC")
    except Exception:
        log.warning("unknown ROUTINE_TZ %r — using UTC", settings.routine_tz)
        return ZoneInfo("UTC")


# ---------- I3: memory upkeep (staleness acts, bounded, budget-aware) ----------


async def _handle_memory_upkeep(ctx: RoutineContext):
    """When a repo's cached staleness crosses the threshold, auto-queue the
    EXISTING memory job kind (two-run bootstrap → draft PR → parks for human
    review — git stays truth). The ONE sanctioned queue interaction of any
    routine. No git/clone work here: staleness comes from the cache the
    engine refreshes after every stage run — an engine-idle repo's staleness
    is FROZEN (under-fires, the safe direction; documented in ENGINE.md §17)."""
    from .memory import MemoryReader  # lazy: keeps module import light

    ws = ctx.workspace
    if ws is None:
        return "skipped", "workspace row missing", 0
    threshold = int(ctx.config.get("staleness_threshold")
                    or ctx.settings.memory_staleness_threshold or 0)
    if threshold <= 0:
        return ("skipped", "inert: staleness threshold 0 (opt-in via "
                "MEMORY_STALENESS_THRESHOLD or the routine config)", 0)
    reader = MemoryReader(ctx.settings)
    cooldown = ctx.settings.memory_upkeep_cooldown_days * 86400
    iso_week = datetime.fromtimestamp(ctx.now, routine_tz(ctx.settings)).strftime("%G-W%V")
    month_start = digests.month_start(ctx.now)
    budget = digests.effective_budget(ws, ctx.settings)
    emitted = queued = 0
    notes: list[str] = []
    for repo in ctx.store.workspace_repos_for(ws["id"]):
        slug = repo["slug"]
        cached = reader.cached(slug) or {}
        meta = cached.get("meta") or {}  # .get chains: {'exists': False} has no meta
        if not cached.get("exists"):
            if ctx.store.inbox_item_add(
                    "routine_note", f"nocache:{slug}",
                    f"no memory cache for {slug}",
                    body=f"Memory upkeep cannot judge `{slug}`: no cached memory "
                         "exists yet. Run a manual bootstrap (Product brain → "
                         "bootstrap) to seed it.",
                    refs={"project": slug}, workspace_id=ws["id"],
                    source="memory_upkeep"):
                emitted += 1
            notes.append(f"{slug}: no cache")
            continue
        staleness = meta.get("staleness_commits")
        if staleness is None or int(staleness) < threshold:
            continue
        fetched_age_d = int((ctx.now - float(meta.get("fetched_at") or ctx.now)) // 86400)
        mem = ctx.store.get(f"mem-{slug}")
        if mem and (mem.get("status") in ACTIVE_JOB_STATUSES
                    or float(mem.get("updated_at") or 0) > ctx.now - cooldown):
            # one per repo per cooldown window — human-triggered bootstraps
            # count toward the bound (the work happened)
            notes.append(f"{slug}: within the {ctx.settings.memory_upkeep_cooldown_days}d bound")
            continue
        if ctx.store.runs_today() >= ctx.settings.max_runs_per_day:
            notes.append(f"{slug}: daily run cap reached — skipped")
            continue
        if budget > 0 and ctx.store.costs_since(month_start).get(ws["id"], 0.0) >= budget:
            notes.append(f"{slug}: monthly budget spent — skipped")
            continue
        decision = ctx.worker.intake_memory(slug, source="routine")
        if "queued" not in decision:
            notes.append(f"{slug}: {decision}")
            continue
        queued += 1
        if ctx.store.inbox_item_add(
                "routine_note", f"memup:{slug}:{iso_week}",
                f"memory refresh queued for {slug} ({staleness} commits stale)",
                body=f"Staleness {staleness} ≥ threshold {threshold} (cache "
                     f"last refreshed {fetched_age_d}d ago). The bootstrap "
                     "parks a draft PR for human review like any other.",
                refs={"project": slug, "job_id": f"mem-{slug}"},
                workspace_id=ws["id"], source="memory_upkeep"):
            emitted += 1
    if queued:
        return "ok", f"queued {queued} refresh(es); " + "; ".join(notes), emitted
    status = "skipped" if notes else "quiet"
    return status, "; ".join(notes) or "all repos fresh", emitted


def _emit(ctx: RoutineContext, draft: dict) -> int:
    """Persist one scanner draft as an inbox item — attributed to the
    emitting routine, deduplicated (and dismissal-remembered) by its key.
    Returns 1 only for a NEW row."""
    new = ctx.store.inbox_item_add(
        draft["kind"], draft["dedupe_key"], draft["title"],
        body=draft.get("body") or "", refs=draft.get("refs") or {},
        workspace_id=ctx.routine.get("workspace_id"),
        source=ctx.routine.get("kind") or "",
        source_sig=draft.get("source_sig") or "")
    return 1 if new else 0


# ---------- I4: risk surfacing ----------


async def _handle_risk_scan(ctx: RoutineContext):
    """Conditions a human should hear about BEFORE asking: Sentry velocity
    spikes, mid-window regressing watch metrics, repeated redos (trust
    decay), spend pacing over budget. Each emits one attributed, deduped
    risk_alert. Scanners inert-by-neutral-thresholds spend nothing."""
    ws = ctx.workspace
    if ws is None:
        return "skipped", "workspace row missing", 0
    s = ctx.settings
    emitted = 0
    notes: list[str] = []
    slugs = {r["slug"] for r in ctx.store.workspace_repos_for(ws["id"])}

    # 1 — Sentry issue-velocity spikes (whole scanner inert without Sentry
    # or a configured threshold; absolute 24h counts — v1 limitation)
    spike_threshold = int(ctx.config.get("sentry_spike_events")
                          or s.risk_sentry_spike_events or 0)
    if spike_threshold > 0 and s.sentry_enabled and ctx.worker is not None:
        try:
            issues = await ctx.worker.sentry.unresolved_issues("24h", 50)
            utc_date = datetime.fromtimestamp(ctx.now, timezone.utc).strftime("%Y-%m-%d")
            for draft in signals.sentry_spikes(issues, slugs, spike_threshold,
                                               utc_date):
                emitted += _emit(ctx, draft)
        except Exception as e:
            notes.append(f"sentry scan failed: {str(e)[:120]}")

    # 2 — regressing watch metrics mid-window (don't wait for day 14)
    for job in ctx.store.by_status(["watching"]):
        if (job.get("kind") or "") != "watch" or job.get("workspace_id") != ws["id"]:
            continue
        readings = ctx.store.readings_for(job["issue_id"],
                                          window_start=job.get("watch_started_at"))
        trend = outcome.mid_window_trend(
            readings, job.get("metric_target") or "",
            int(job.get("metric_window_days") or 0), s.outcome_flat_band_pct)
        draft = signals.watch_regression_alert(job, trend)
        if draft:
            emitted += _emit(ctx, draft)

    # 3 — repeated redos on the same stage (trust decaying)
    redo_threshold = int(ctx.config.get("redo_threshold")
                         or s.risk_redo_threshold or 0)
    redo_rows = [r for r in ctx.store.redo_counts(
        ctx.now - s.autonomy_window_days * 86400)
        if r.get("workspace_id") == ws["id"]]
    for draft in signals.redo_alerts(redo_rows, redo_threshold):
        emitted += _emit(ctx, draft)

    # 4 — spend pacing above budget (early-month guarded, pct-bucketed key)
    budget = digests.effective_budget(ws, s)
    spend = ctx.store.costs_since(digests.month_start(ctx.now)).get(ws["id"], 0.0)
    draft = signals.spend_alert(ws["id"], spend, budget, ctx.now)
    if draft:
        emitted += _emit(ctx, draft)

    detail = "; ".join(notes) if notes else f"{emitted} new alert(s)"
    return ("ok" if emitted or notes else "quiet"), detail, emitted


# ---------- I5: the proposal lane (parked candidate briefs) ----------


def _has_live_successor(store: JobStore, feature_id: str) -> bool:
    """Is the feature being re-lapped, or does an ACTIVE job reference it in
    related_jobs? Then no iterate proposal — the work is already moving."""
    feat = store.get(feature_id)
    if feat and (feat.get("status") or "") in ACTIVE_JOB_STATUSES:
        return True
    for j in store.jobs_with_related():
        if j["issue_id"] == feature_id:
            continue
        related = {x.strip() for x in (j.get("related_jobs") or "").split(",")
                   if x.strip()}
        if feature_id in related and (j.get("status") or "") in ACTIVE_JOB_STATUSES:
            return True
    return False


async def _handle_proposal_scan(ctx: RoutineContext):
    """Parked candidate BRIEFS a human adopts or dismisses — never
    self-initiated pipelines (adoption via POST /api/inbox/notices/{id}/adopt
    is the only path to intake). Dismissals are remembered by the dedupe
    keys; the count-bucketed families additionally honor a source-signature
    recency guard so a dismissal holds for a full PROPOSAL_WINDOW_DAYS."""
    from .memory import MemoryReader  # lazy, like memory_upkeep

    ws = ctx.workspace
    if ws is None:
        return "skipped", "workspace row missing", 0
    s = ctx.settings
    window = s.proposal_window_days * 86400
    since = ctx.now - window
    emitted = 0

    def emit_guarded(drafts, recency_guard=True):
        nonlocal emitted
        for draft in drafts:
            if recency_guard and ctx.store.inbox_item_recent_sig(
                    "proposal", draft.get("source_sig") or "", since):
                continue
            emitted += _emit(ctx, draft)

    # 1 — outcome verdicts (flat/regressed, decided, no live successor)
    outcome_rows = []
    for row in ctx.store.outcomes_decided():
        if row.get("workspace_id") != ws["id"]:
            continue
        feat = ctx.store.get(row.get("feature_id") or "") or {}
        outcome_rows.append(row | {"project": feat.get("project") or ""})
    emit_guarded(signals.outcome_proposals(
        outcome_rows, lambda fid: _has_live_successor(ctx.store, fid)),
        recency_guard=False)  # the key already folds verdict+learning

    # 2 — the friction log (recurring process pain)
    frictions = ctx.store.frictions_since(since, ws["id"])
    emit_guarded(signals.friction_proposals(
        frictions, int(s.proposal_friction_min or 0)))

    # 3 — sentry clusters (pure DB; pre-upgrade rows have culprit='' and are
    # skipped — clusters accumulate from upgrade forward)
    sentry_rows = [r for r in ctx.store.sentry_jobs_since(since)
                   if r.get("workspace_id") == ws["id"]]
    emit_guarded(signals.sentry_cluster_proposals(
        sentry_rows, int(s.proposal_sentry_cluster_min or 0)))

    # 4 — stale high-traffic memory areas. Needs a configured staleness
    # threshold (0 = no basis to judge — skipped); proposes only where the
    # upkeep routine won't act: its weekly bound already spent, or the
    # routine itself inert/disabled.
    threshold = int(s.memory_staleness_threshold or 0)
    if threshold > 0:
        upkeep = next((r for r in ctx.store.routines_all()
                       if r.get("workspace_id") == ws["id"]
                       and r["kind"] == "memory_upkeep"), None)
        upkeep_active = bool(upkeep and upkeep.get("enabled")
                             and s.routines_enabled)
        cooldown = s.memory_upkeep_cooldown_days * 86400
        reader = MemoryReader(s)
        staleness_map = {}
        for repo in ctx.store.workspace_repos_for(ws["id"]):
            slug = repo["slug"]
            cached = reader.cached(slug) or {}
            staleness = (cached.get("meta") or {}).get("staleness_commits")
            if staleness is None:
                continue
            mem = ctx.store.get(f"mem-{slug}")
            bound_spent = bool(mem and float(mem.get("updated_at") or 0)
                               > ctx.now - cooldown)
            if upkeep_active and not bound_spent:
                continue  # I3 will act on this repo itself
            staleness_map[slug] = int(staleness)
        emit_guarded(signals.memory_proposals(
            staleness_map, ctx.store.jobs_count_by_project(since), threshold))

    return ("ok" if emitted else "quiet"), f"{emitted} new proposal(s)", emitted


# ---------- I2: the daily standup digest ----------


async def _handle_standup(ctx: RoutineContext):
    ws = ctx.workspace
    if ws is None:
        return "skipped", "workspace row missing", 0
    # `since` = the last useful (ok|quiet) standup run, floored at now-24h:
    # a first run or a long outage must never replay full history.
    anchor = ctx.store.routine_last_success(ctx.routine["id"])
    since = max(anchor or 0.0, ctx.now - 86400)
    digest = digests.build_standup(ctx.store, ctx.settings, ws, since, ctx.now)
    if digest is None:
        return "quiet", "nothing to report", 0
    tz = routine_tz(ctx.settings)
    local_date = datetime.fromtimestamp(ctx.now, tz).strftime("%Y-%m-%d")
    key = f"{ws['id']}:{local_date}"  # a mid-day run-now re-fire is idempotent
    new = ctx.store.inbox_item_add(
        "standup_digest", key, digest["title"], digest["body"],
        refs={"date": local_date}, workspace_id=ws["id"],
        source="standup_digest")
    if new:
        row = ctx.store.inbox_item_by_key("standup_digest", key)
        if row:  # yesterday's unread digest expires the moment today's lands
            ctx.store.inbox_expire_predecessors("standup_digest", ws["id"],
                                                row["id"])
        # DB row first, best-effort Slack strictly after (crash under-notifies,
        # never double-fires — the Epic A5 ordering)
        if ctx.workspaces is not None:
            await ctx.workspaces.notify_text(
                ws, f"*{digest['title']}*\n\n{digest['body'][:3500]}")
    n_lines = sum(len(v) for v in digest["sections"].values())
    return "ok", f"{len(digest['sections'])} section(s), {n_lines} line(s)", \
        1 if new else 0


# ---------- I6: the weekly planning pack ----------


async def _handle_weekly_planning(ctx: RoutineContext):
    """Assemble the weekly review mechanically (no model run; the next-lap
    ranking is a transparent formula stated in the pack body). Always
    produces a pack — the weekly cadence is the point, unlike the
    exception-only standup. GET /api/inbox carries it; no bespoke endpoint
    (deliberate)."""
    ws = ctx.workspace
    if ws is None:
        return "skipped", "workspace row missing", 0
    pack = digests.build_planning_pack(ctx.store, ctx.settings, ws, ctx.now)
    tz = routine_tz(ctx.settings)
    iso_week = datetime.fromtimestamp(ctx.now, tz).strftime("%G-W%V")
    key = f"{ws['id']}:{iso_week}"
    new = ctx.store.inbox_item_add(
        "planning_pack", key, pack["title"], pack["body"],
        refs={"week": iso_week, "ranked": pack["ranked"]},
        workspace_id=ws["id"], source="weekly_planning")
    if new:
        row = ctx.store.inbox_item_by_key("planning_pack", key)
        if row:
            ctx.store.inbox_expire_predecessors("planning_pack", ws["id"],
                                                row["id"])
        if ctx.workspaces is not None:
            await ctx.workspaces.notify_text(
                ws, f"*{pack['title']}*\n\n{pack['body'][:3500]}")
    return "ok", f"week {iso_week}", 1 if new else 0


REGISTRY: dict = {
    "sweep": _handle_sweep,
    "reaper": _handle_reaper,
    "janitor": _handle_janitor,
    "standup_digest": _handle_standup,
    "memory_upkeep": _handle_memory_upkeep,
    "risk_scan": _handle_risk_scan,
    "proposal_scan": _handle_proposal_scan,
    "weekly_planning": _handle_weekly_planning,
}


# ---------- seeding ----------


def anchor_fresh_rows(store: JobStore, now: float | None = None):
    """Stamp last_run_at=now on never-run daily/weekly rows. Their next_due
    anchors on last_run_at; a NULL anchor re-slides to `now` at every tick,
    so the row would never come due (the seeded standup / memory-upkeep /
    weekly-planning rows would silently never fire). Stamping at seed time
    keeps the fresh-seed contract — the row fires at its next occurrence
    after this moment, never a missed slot. every: rows keep NULL history
    (= due immediately, by design); builtin rows (schedule='' — derive from
    settings) are every:-shaped and skipped."""
    now = now if now is not None else _time.time()
    for row in store.routines_all():
        if row.get("last_run_at") is not None:
            continue
        spec = (row.get("schedule") or "").strip()
        if not spec:
            continue
        try:
            sched = parse_schedule(spec)
        except ValueError:
            continue  # the dispatch paths handle bad schedules fail-closed
        if sched.kind in ("daily", "weekly"):
            store.routine_set(row["id"], last_run_at=now)


def ensure_seeds_for_workspace(store: JobStore, settings: Settings,
                               workspace_id: int):
    """Seed the per-workspace Epic I rows (INSERT OR IGNORE — operator edits
    survive re-seeds). All enabled but individually inert-by-neutral-
    thresholds where they'd spend anything."""
    seeds = {
        "standup_digest": settings.standup_schedule,
        "memory_upkeep": settings.memory_upkeep_schedule,
        "risk_scan": settings.risk_scan_schedule,
        "proposal_scan": settings.proposal_scan_schedule,
        "weekly_planning": settings.planning_schedule,
    }
    for kind, schedule in seeds.items():
        store.routine_upsert_seed(kind, workspace_id, schedule, name=kind)
    anchor_fresh_rows(store)


def ensure_seeds(store: JobStore, settings: Settings):
    """Called from lifespan after WorkspaceService.ensure_default(): builtin
    instance rows (schedule='' = derive from settings) + per-workspace Epic I
    rows, then the EVERY-boot settle bump for sweep/janitor (distinct from
    seeding — see routine_boot_bump). anchor_fresh_rows also backfills
    daily/weekly rows seeded by older builds with a NULL last_run_at."""
    for kind in BUILTIN_KINDS:
        store.routine_upsert_seed(kind, None, "", name=kind)
    for ws in store.workspace_list():
        ensure_seeds_for_workspace(store, settings, ws["id"])
    store.routine_boot_bump(("sweep", "janitor"))
    anchor_fresh_rows(store)


# ---------- the scheduler ----------


class RoutineScheduler:
    def __init__(self, settings: Settings, store: JobStore, worker,
                 workspaces=None):
        self.settings = settings
        self.store = store
        self.worker = worker
        self.workspaces = workspaces
        self._wake = asyncio.Event()
        self._run_now: set[int] = set()
        self._inflight: set[int] = set()
        self._tasks: set = set()
        self.last_tick: float = _time.time()  # F4: heartbeat for /health/ready

    # -- public --

    def request_run(self, routine_id: int) -> bool:
        """Arm an immediate fire; the scheduler task picks it up on its next
        tick (amendment 10: handlers never run inline in an HTTP request).
        Returns False for unknown/unregistered/disabled routines."""
        row = self.store.routine_get(routine_id)
        if row is None or row["kind"] not in REGISTRY:
            return False
        if not self._effective_enabled(row):
            return False
        self._run_now.add(int(routine_id))
        self._wake.set()
        return True

    async def run_forever(self):
        log.info("routine scheduler started (%d registered kinds)", len(REGISTRY))
        # sleep-first, like the loops this engine replaced: the boot settle
        # belongs to the boot bump + _recover_interrupted, not a dispatch
        # storm in the first event-loop tick. request_run wakes us early.
        while True:
            self.last_tick = _time.time()  # F4 heartbeat
            try:
                await asyncio.wait_for(self._wake.wait(),
                                       timeout=self.settings.routine_tick_seconds)
                self._wake.clear()
            except asyncio.TimeoutError:
                pass
            try:
                self._dispatch_due()
            except Exception:
                log.exception("routine tick failed")

    # -- internals --

    def _tz(self):
        try:
            return ZoneInfo(self.settings.routine_tz or "UTC")
        except Exception:
            log.warning("unknown ROUTINE_TZ %r — using UTC", self.settings.routine_tz)
            return ZoneInfo("UTC")

    def _effective_enabled(self, row: dict) -> bool:
        """Row toggle AND legacy settings flags AND the Epic I master flag —
        either off means off (fail closed), EXCEPT the reaper, which is a
        safety loop and non-disableable by design."""
        kind = row["kind"]
        if kind == "reaper":
            return True
        if not row.get("enabled"):
            return False
        if row.get("workspace_id") is None:
            if kind == "sweep":
                return bool(self.settings.sweep_enabled and self.settings.sentry_enabled)
            return True  # janitor
        return bool(self.settings.routines_enabled)

    def resolve_schedule(self, row: dict) -> Schedule | None:
        """The row's effective schedule. Builtin rows: stored non-empty
        schedule wins, else (or on a parse failure — logged, never a silently
        dead reaper) the settings-derived default. Workspace rows: a bad
        schedule disables the routine fail-closed."""
        spec = (row.get("schedule") or "").strip()
        builtin = row.get("workspace_id") is None and row["kind"] in BUILTIN_KINDS
        if builtin:
            default = builtin_default_schedule(row["kind"], self.settings)
            if not spec:
                return parse_schedule(default)
            try:
                return parse_schedule(spec)
            except ValueError as e:
                log.error("builtin routine %s has a bad schedule %r (%s) — "
                          "falling back to %s", row["kind"], spec, e, default)
                return parse_schedule(default)
        try:
            return parse_schedule(spec)
        except ValueError:
            if row.get("enabled"):
                self.store.routine_set(row["id"], enabled=0,
                                       last_status="error: bad schedule")
                log.error("routine %s#%s disabled: bad schedule %r",
                          row["kind"], row["id"], spec)
            return None

    def _dispatch_due(self):
        now = _time.time()
        tz = self._tz()
        for row in self.store.routines_all():
            kind = row["kind"]
            handler = REGISTRY.get(kind)
            if handler is None:
                continue  # unknown kind (older build) — skip quietly
            if not self._effective_enabled(row):
                self._run_now.discard(row["id"])
                continue
            sched = self.resolve_schedule(row)
            if sched is None:
                continue
            forced = row["id"] in self._run_now
            if not forced and now < next_due(sched, row["last_run_at"], now, tz):
                continue
            if row["id"] in self._inflight:
                continue  # in-flight guard: never two concurrent firings
            if not self.store.routine_claim(row["id"], row["last_run_at"], now,
                                            ignore_disabled=(kind == "reaper")):
                self._run_now.discard(row["id"])
                continue  # another scheduler won the claim
            self._run_now.discard(row["id"])
            self._inflight.add(row["id"])
            task = asyncio.create_task(self._run_one(dict(row), handler, now))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_one(self, row: dict, handler, now: float):
        """One firing: run-history row, handler with try/except, close +
        last_status. A raise records 'error'; the next due tick retries.
        EVERY line here — including routine_run_open and the closing store
        writes, which can raise on transient DB trouble ('database is
        locked', disk full) — runs under the finally that discards the id
        from _inflight: a leaked id would block the routine (including the
        non-disableable reaper) forever, until a process restart."""
        status, detail, items = "error", "", 0
        run_id = None
        try:
            run_id = self.store.routine_run_open(row["id"], row["kind"],
                                                 row.get("workspace_id"))
            try:
                config = json.loads(row.get("config") or "{}")
                if not isinstance(config, dict):
                    config = {}
            except (ValueError, TypeError):
                config = {}
            ctx = RoutineContext(settings=self.settings, store=self.store,
                                 worker=self.worker, workspaces=self.workspaces,
                                 routine=row, config=config, now=now)
            result = await handler(ctx)
            if isinstance(result, tuple) and len(result) == 3:
                status, detail, items = result
            else:
                status, detail, items = "ok", "", 0
        except Exception as e:
            log.exception("routine %s#%s failed", row["kind"], row["id"])
            status, detail = "error", str(e)[:300]
        finally:
            try:
                if run_id is not None:
                    self.store.routine_run_close(run_id, status, detail, items)
                self.store.routine_set(row["id"], last_status=status,
                                       last_result=(detail or "")[:500])
            except Exception:
                log.exception("routine %s#%s: closing store writes failed",
                              row["kind"], row["id"])
            finally:
                self._inflight.discard(row["id"])

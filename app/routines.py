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
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from . import digests
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
    occurrence (a fresh seed must not fire a missed slot at boot)."""
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


REGISTRY: dict = {
    "sweep": _handle_sweep,
    "reaper": _handle_reaper,
    "janitor": _handle_janitor,
    "standup_digest": _handle_standup,
}


# ---------- seeding ----------


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


def ensure_seeds(store: JobStore, settings: Settings):
    """Called from lifespan after WorkspaceService.ensure_default(): builtin
    instance rows (schedule='' = derive from settings) + per-workspace Epic I
    rows, then the EVERY-boot settle bump for sweep/janitor (distinct from
    seeding — see routine_boot_bump)."""
    for kind in BUILTIN_KINDS:
        store.routine_upsert_seed(kind, None, "", name=kind)
    for ws in store.workspace_list():
        ensure_seeds_for_workspace(store, settings, ws["id"])
    store.routine_boot_bump(("sweep", "janitor"))


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
        while True:
            try:
                self._dispatch_due()
            except Exception:
                log.exception("routine tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(),
                                       timeout=self.settings.routine_tick_seconds)
                self._wake.clear()
            except asyncio.TimeoutError:
                pass

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
        last_status. A raise records 'error'; the next due tick retries."""
        run_id = self.store.routine_run_open(row["id"], row["kind"],
                                             row.get("workspace_id"))
        status, detail, items = "error", "", 0
        try:
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
            self._inflight.discard(row["id"])
            self.store.routine_run_close(run_id, status, detail, items)
            self.store.routine_set(row["id"], last_status=status,
                                   last_result=(detail or "")[:500])

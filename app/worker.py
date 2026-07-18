"""Serial job worker: SQLite is the queue of record; the asyncio queue is a
wakeup signal (docs/ENGINE.md §5).

Job kinds:
- sentry:  grade -> fix (HITL only when Claude judges it COMPLEX)
- task:    analyse -> gate -> implement
- feature: P0-P9 pipeline, gate after every stage (delegated to Engine)
- memory:  product-memory bootstrap -> draft PR

Background loops: ClickUp poller (gate answers by comment, on the parent task
or any artifact subtask), sweep, stale-run reaper.
"""

import asyncio
import itertools
import json
import logging
import re
import time
import uuid
from pathlib import Path

from . import analytics, autonomy, outcome, people, roles
from .clickup import ClickUp
from .config import ENGINE_NAME, Settings
from .db import JobStore
from .engine import ENGINE_COMMENT_PREFIXES, GATE_PREFIX, Engine, RepoLocks
from .fixer import (
    BASE_ALLOWED_TOOLS,
    TRANSIENT_ERROR_RE,
    BranchLostError,
    engine_dir,
    git,
    prepare_feature_workspace,
    prepare_workspace,
    run_claude,
    run_claude_raw,
)
from .grading import grade_issue
from .prompts import (
    build_fix_prompt,
    build_phase2_prompt,
    build_shepherd_prompt,
    build_task_implement_prompt,
    build_task_plan_prompt,
)
from .sentry_api import SentryClient, format_stacktrace
from .textutil import single_line
from . import transcripts

log = logging.getLogger("brain.worker")

ACTIVE_STATUSES = ("received", "queued", "running")
TERMINAL_STATUSES = ("pr_opened", "no_fix", "skipped", "error", "timeout", "done")

# a watch job reads its metric at most ~daily; 22h tolerates loop jitter
WATCH_READ_THROTTLE_SECONDS = 79200

# priority classes: live sentry >= answered feature stages / tasks > sweep
PRIO_SENTRY = 0
PRIO_HUMAN = 1
PRIO_SWEEP = 2

QUESTION_HEADING_RE = re.compile(r"^#{1,4}\s*(?:open\s+)?questions?\b.*$", re.IGNORECASE | re.MULTILINE)
REDO_TARGET_RE = re.compile(r"^\s*[Pp](\d)\b\s*")

# ClickUp intake: '[fix] title', '[feature] title', '[sentry 123456] title',
# '[memory <project>] title'
INTAKE_RE = re.compile(r"^\s*\[\s*(fix|task|bug|feature|sentry|memory)(?:\s+([A-Za-z0-9_-]+))?\s*\]\s*(.+)$",
                       re.IGNORECASE)
PROJECT_LINE_RE = re.compile(r"^\s*\**project\**\s*[:=]\s*\**\s*([A-Za-z0-9_-]+)\**\s*$",
                             re.IGNORECASE | re.MULTILINE)
# Epic B1: metric goal lines in a [feature] ticket description, the same
# bold-tolerant shape as the project line; matched lines strip from `request`
METRIC_LINE_RE = re.compile(r"^\s*\**metric\**\s*[:=]\s*(.+)$",
                            re.IGNORECASE | re.MULTILINE)
TARGET_LINE_RE = re.compile(r"^\s*\**target\**\s*[:=]\s*(.+)$",
                            re.IGNORECASE | re.MULTILINE)
WINDOW_LINE_RE = re.compile(r"^\s*\**window\**\s*[:=]\s*\**\s*(\d{1,3})\**\s*$",
                            re.IGNORECASE | re.MULTILINE)


def _clean_metric_value(raw: str) -> str:
    """Strip ClickUp's bold markers and whitespace off a captured line value."""
    return (raw or "").strip().strip("*").strip()


# Metric/target values are interpolated into engine-voiced prompt headers on
# EVERY stage run: a multiline value could smuggle markdown headings/
# instructions into the prompt as if the engine had written them, so the
# stored value is forced single-line and capped at intake (both channels
# funnel through here). Moved to textutil (Epic I) — this alias stays for
# existing call sites and tests.
_single_line = single_line


class GateConflict(Exception):
    """The gate was already answered through the other channel."""


class GateForbidden(Exception):
    """The acting user does not own this gate (Epic A3). Deliberately NOT a
    ValueError subclass: main.py maps ValueError->409 and _scan_verbs catches
    (ValueError, KeyError) with a generic reply — this must surface as a 403 /
    ownership refusal, never be swallowed by those handlers."""


def extract_questions(analysis: str) -> str:
    """Pull the `## Questions` section out of an analysis for the dashboard.
    Takes the LAST questions heading — stage payloads may embed earlier ones."""
    matches = list(QUESTION_HEADING_RE.finditer(analysis or ""))
    if matches:
        rest = analysis[matches[-1].end():]
        nxt = re.search(r"^#{1,4}\s", rest, re.MULTILINE)
        section = (rest[: nxt.start()] if nxt else rest).strip()
        if section:
            return section[:1500]
    return (analysis or "").strip()[-600:]


class Worker:
    def __init__(self, settings: Settings, store: JobStore):
        self.settings = settings
        self.store = store
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self.sentry = SentryClient(settings)
        self.clickup = ClickUp(settings)
        self.locks = RepoLocks()
        self.engine = Engine(settings, store, self.clickup, locks=self.locks)
        self.workspaces = None  # WorkspaceService, injected at startup (main.lifespan)
        # strong refs for fire-and-forget background tasks (outcome memory PRs) —
        # bare create_task results can be GC'd mid-flight (same as main._chat_tasks)
        self._bg_tasks: set = set()

    def _enqueue(self, job_id: str, priority: int):
        self.queue.put_nowait((priority, next(self._seq), job_id, time.time()))

    def _priority_for(self, job: dict) -> int:
        kind = job.get("kind") or "sentry"
        if kind == "sentry":
            return PRIO_SWEEP if job.get("source") == "sweep" else PRIO_SENTRY
        return PRIO_HUMAN

    # ---------- intake ----------

    def intake(self, issue_id: str, source: str, forced: bool = False,
               title: str = "", project: str = "") -> str:
        """Sentry issue guardrails + enqueue; returns a human-readable decision."""
        existing = self.store.get(issue_id)
        if existing:
            if existing["status"] in ACTIVE_STATUSES:
                return f"issue {issue_id} already in progress ({existing['status']})"
            if existing["status"] == "awaiting_input":
                return f"issue {issue_id} is awaiting human input on {existing['clickup_task_url'] or 'its ticket'}"
            if existing["status"] == "pr_opened":
                return f"issue {issue_id} already has a PR: {existing['pr_url']}"
            cooldown = self.settings.issue_cooldown_hours * 3600
            if not forced and time.time() - existing["updated_at"] < cooldown:
                return f"issue {issue_id} in cooldown ({existing['status']})"

        self.store.insert(issue_id, source=source, forced=forced, title=title, project=project)
        self._stamp_workspace(issue_id, project)
        self._enqueue(issue_id, PRIO_SWEEP if source == "sweep" else PRIO_SENTRY)
        return f"issue {issue_id} queued"

    def _job_context(self, job: dict) -> tuple[str, str]:
        """(product_name, briefing) for a job's prompts — workspace-aware when
        the service is injected, instance-wide otherwise (§10/§12)."""
        if self.workspaces:
            ws = self.workspaces.for_job(job)
            return self.workspaces.product_name_for(ws), self.workspaces.briefing(ws)
        return self.settings.product_name, self.settings.business_context

    def _ws_row(self, job: dict) -> dict | None:
        """The job's workspace row (attribution/role/SLA config), when the
        service is injected — mirrors _job_context."""
        return self.workspaces.for_job(job) if self.workspaces else None

    def _stamp_workspace(self, job_id: str, project: str):
        """Record the owning workspace on the job row (slugs are global)."""
        if self.workspaces and project:
            ws = self.workspaces.for_project(project)
            if ws:
                self.store.set_fields(job_id, workspace_id=ws["id"])

    def intake_task(self, job_id: str, title: str, project: str, request: str,
                    clickup_task_id: str | None = None,
                    clickup_task_url: str | None = None) -> str:
        """Enqueue a manually reported request (bug fix / change request)."""
        existing = self.store.get(job_id)
        if existing:
            if existing["status"] in ACTIVE_STATUSES:
                return f"request {job_id} already in progress ({existing['status']})"
            if existing["status"] == "awaiting_input":
                return f"request {job_id} is awaiting your input on {existing['clickup_task_url'] or 'its ticket'}"
            if existing["status"] == "pr_opened":
                return f"request {job_id} already has a PR: {existing['pr_url']}"

        self.store.insert(job_id, source="manual", forced=True,
                          title=title, project=project, kind="task")
        self.store.set_fields(
            job_id,
            request=request,
            clickup_task_id=clickup_task_id or "",
            clickup_task_url=clickup_task_url or "",
        )
        self._stamp_workspace(job_id, project)
        self._enqueue(job_id, PRIO_HUMAN)
        return f"request {job_id} queued"

    def intake_feature(self, job_id: str, title: str, project: str, request: str,
                       clickup_task_id: str | None = None,
                       clickup_task_url: str | None = None,
                       cu_list_id: str = "", owner: str = "",
                       founder_dri: str = "", dev_dri: str = "",
                       related_jobs: str = "", gate_mode: str = "",
                       success_metric: str = "", metric_target: str = "",
                       metric_window_days: int | None = None) -> str:
        """Enqueue a feature pipeline at P0. Dual DRIs (Epic A2): the legacy
        `owner` column becomes a computed alias at write time. A re-intake
        resets ALL of them from the fresh submission (consistent with the
        atomic pipeline-reset contract). NOTE (Epic D1): with
        PEOPLE_ROUTING_DEFAULTS on and people profiles covering the repo, an
        EMPTY slot re-fills from the profiles — resubmitting without DRIs no
        longer guarantees enforcement turns off; PEOPLE_ROUTING_DEFAULTS=false
        is the opt-out (ENGINE.md §16). Explicit submitted values always win;
        the fill is computed BEFORE store.feature_intake so the DRIs land
        inside the single atomic upsert (never a second write)."""
        existing = self.store.get(job_id)
        if existing:
            if existing["status"] in ACTIVE_STATUSES:
                return f"feature {job_id} already in progress ({existing['status']})"
            if existing["status"] == "awaiting_input":
                return (f"feature {job_id} is parked at its P{existing['stage']} gate — "
                        "answer it instead of resubmitting")
            if existing["status"] in ("error", "timeout"):
                return (f"feature {job_id} hit {existing['status']} at P{existing['stage']} — "
                        "use redo (dashboard re-kick or `/redo` on the ticket) to resume")
            if existing["status"] == "pr_opened":
                return f"feature {job_id} already shipped: {existing['pr_url']}"
            # terminal skipped/no_fix -> fresh restart of the pipeline (atomic below)

        mode = gate_mode if gate_mode in ("full", "light") else self.settings.default_gate_mode
        if metric_window_days is not None and not 1 <= int(metric_window_days) <= 365:
            metric_window_days = None  # fail closed: never store a nonsense window
        founder_dri = (founder_dri or "").strip()
        dev_dri = (dev_dri or "").strip()
        # Epic D1: people-profile routing defaults fill ONLY empty slots —
        # explicit submitted values (dashboard AND ClickUp adoption's people
        # fields, both funneling here) always win. Guarded for bare-Worker
        # tests (no workspace service → no membership check → no fill).
        if (self.settings.people_routing_defaults and self.workspaces
                and (not founder_dri or not dev_dri)):
            ws = self.workspaces.for_project(project)
            d_founder, d_dev = people.default_dris(self.store, ws, project)
            founder_dri = founder_dri or d_founder
            dev_dri = dev_dri or d_dev
        owner = (owner or "").strip() or dev_dri or founder_dri  # legacy computed alias
        self.store.feature_intake(
            job_id, title=title, project=project,
            request=request,
            stage=0,
            stage_attempts=0,
            pending_redo_stage=None,
            analysis=None,
            question="",
            evidence="",
            pr_url=None,
            resume_session_id="",
            resume_stage=None,
            resume_attempt=None,
            resume_head="",
            resume_answer="",
            gate_kind="",
            ask_count=0,
            gate_mode=mode if mode in ("full", "light") else "full",
            clickup_task_id=clickup_task_id or "",
            clickup_task_url=clickup_task_url or "",
            cu_list_id=cu_list_id,
            owner=owner,
            founder_dri=founder_dri,
            dev_dri=dev_dri,
            related_jobs=related_jobs,
            # Epic B1: metric goal from the fresh submission (a re-intake
            # overwrites, consistent with the atomic pipeline reset) — and the
            # previous lap's engine-owned metric/watch state clears with it.
            # Single-line + capped: these values render inside engine-voiced
            # prompt headers (feature_prompts._metric_goal_block) — see
            # _single_line for why multiline/unbounded values are refused.
            success_metric=_single_line(success_metric),
            metric_target=_single_line(metric_target),
            metric_window_days=metric_window_days,
            metric_event="",
            watch_started_at=None,
            watch_deadline=None,
        )
        self._stamp_workspace(job_id, project)
        self._enqueue(job_id, PRIO_HUMAN)
        return f"feature {job_id} queued at P0"

    def intake_memory(self, project: str, source: str = "manual") -> str:
        """source='routine' records upkeep-queued bootstraps' provenance
        (Epic I3); grading/caps are unaffected — memory jobs bypass grading."""
        job_id = f"mem-{project}"
        existing = self.store.get(job_id)
        if existing and existing["status"] in ACTIVE_STATUSES:
            return f"memory bootstrap for {project} already in progress ({existing['status']})"
        self.store.insert(job_id, source=source, forced=True,
                          title=f"memory bootstrap: {project}", project=project, kind="memory")
        self._stamp_workspace(job_id, project)
        self._enqueue(job_id, PRIO_HUMAN)
        return f"memory bootstrap for {project} queued"

    # ---------- main loop ----------

    async def run_forever(self):
        await self.clickup.load_statuses()
        # SQLite is the queue of record: re-enqueue whatever a restart dropped
        for job in self.store.requeueable():
            self._enqueue(job["issue_id"], self._priority_for(job))
            log.info("startup requeue: %s (%s)", job["issue_id"], job["status"])
        await self._recover_interrupted()
        log.info("worker started")
        while True:
            _, _, job_id, queued_at = await self.queue.get()
            try:
                job = self.store.get(job_id)
                if job is None or job["status"] not in ("received", "queued"):
                    continue  # stale wakeup — the DB row moved on
                await self._process(job, queued_at)
            except Exception as e:
                log.exception("job %s failed", job_id)
                self.store.set_status(job_id, "error", detail=str(e)[:2000])
                row = self.store.get(job_id) or {}
                await self.clickup.comment(
                    row.get("clickup_task_id") or "",
                    f"{GATE_PREFIX} internal error on this job: {str(e)[:500]}",
                )
            finally:
                self.queue.task_done()

    async def _recover_interrupted(self):
        """Boot-time crash recovery: at startup NO run can legitimately be
        'running' — the CLI subprocess dies with the process (deploys restart
        the container mid-run). Requeue those corpses immediately instead of
        letting the stale-run reaper surface them as errors an hour later;
        the stage/phase machinery re-runs them cleanly (attempts bump, fresh
        checkout). The reaper stays for mid-life zombies in a live process."""
        for job in self.store.by_status(["running"]):
            self.store.set_status(
                job["issue_id"], "queued",
                detail="recovered: a restart interrupted the previous run — re-running")
            self._enqueue(job["issue_id"], self._priority_for(job))
            log.info("startup recovery: requeued interrupted run %s", job["issue_id"])
            await self.clickup.comment(
                job.get("clickup_task_id") or "",
                f"{GATE_PREFIX} ♻️ a service restart interrupted the run in progress — "
                "it has been requeued and will re-run automatically.")

    async def _process(self, job: dict, queued_at: float | None = None):
        """Every workspace toucher runs under its repo's lock (chat runs and the
        canonical product-scope reads take the same locks — see Engine.RepoLocks)."""
        kind = job.get("kind") or "sentry"
        if kind == "watch":
            # fail closed, forever: a watch job must NEVER reach a Claude run —
            # the watch loop owns its whole lifecycle. Any path that queues one
            # (stale wakeup, future bug) lands here and is handed back.
            log.warning("watch job %s reached the run queue — returning it to the "
                        "watch loop, no Claude run", job["issue_id"])
            self.store.set_status(job["issue_id"], "watching",
                                  detail="watch jobs never run Claude — returned to "
                                         "the watch loop")
            return
        if kind == "sentry":
            # a fresh sentry job's project isn't known until the issue is fetched;
            # _process_sentry acquires the repo lock itself once it is
            await self._process_sentry(job)
            return
        target = self.settings.repo_for_project(job.get("project") or "")
        if target is None:
            self.store.set_status(job["issue_id"], "skipped",
                                  detail=f"no repo mapped for '{job.get('project')}'")
            return
        async with self.locks.for_repo(target.repo):
            if kind == "feature":
                result = await self.engine.run_stage(job, queued_at)
                if result == "requeue":  # e.g. P6 auto-skip advanced the stage
                    self._enqueue(job["issue_id"], PRIO_HUMAN)
            elif kind == "memory":
                await self.engine.run_memory_bootstrap(job)
            else:
                await self._process_task(job)

    # ---------- sentry flow (v1) ----------

    async def _process_sentry(self, row: dict):
        issue_id = row["issue_id"]
        phase = int(row.get("phase") or 1)
        forced = bool(row.get("forced"))

        issue = await self.sentry.issue(issue_id)
        project_slug = (issue.get("project") or {}).get("slug", "")
        self.store.set_fields(
            issue_id,
            title=issue.get("title", "unknown")[:300],
            project=project_slug,
            issue_url=issue.get("permalink", ""),
        )
        # webhook intake has no project yet — stamp the workspace the moment
        # the slug is known (before any skip path), or webhook-sourced jobs
        # stay invisible to workspace members forever (sentry finding 1595670)
        self._stamp_workspace(issue_id, project_slug)

        if phase == 1:
            grade = grade_issue(issue, self.settings, forced=forced)
            self.store.set_fields(issue_id, score=grade.score, grade_reasons=grade.summary)
            if not grade.accept:
                self.store.set_status(issue_id, "skipped", detail=grade.summary)
                log.info("issue %s skipped by grading: %s", issue_id, grade.summary)
                return

        if self.store.runs_today() >= self.settings.max_runs_per_day and not forced:
            self.store.set_status(
                issue_id, "skipped",
                detail=f"daily cap of {self.settings.max_runs_per_day} Claude runs reached",
            )
            return

        target = self.settings.repo_for_project(project_slug)
        if target is None:
            self.store.set_status(issue_id, "skipped", detail=f"no repo mapped for '{project_slug}'")
            return

        row = self.store.get(issue_id)
        task_id = row.get("clickup_task_id")
        if not task_id:
            cu_on, cu_list = (self.workspaces.clickup_route(project_slug)
                              if self.workspaces else (True, None))
            created = None
            if cu_on:
                created = await self.clickup.create_task(
                    name=f"[{project_slug}] {issue.get('title', 'unknown')}",
                    description=self._ticket_description(issue, row),
                    list_id=cu_list,
                )
            if created:
                task_id, task_url = created
                self.store.set_fields(issue_id, clickup_task_id=task_id, clickup_task_url=task_url)
                row = self.store.get(issue_id)

        self.store.set_fields(issue_id, run_started_at=time.time())
        self.store.set_status(issue_id, "running")
        await self.clickup.set_status(task_id or "", "running")

        event = await self.sentry.latest_event(issue_id)
        issue_info = {
            "id": issue_id,
            "title": issue.get("title", "unknown"),
            "url": issue.get("permalink", ""),
            "culprit": issue.get("culprit", "unknown"),
            "times_seen": str(issue.get("count", "?")),
            "users_affected": str(issue.get("userCount", "?")),
            "project": project_slug,
        }
        stacktrace = format_stacktrace(event)
        # stored branch wins (phase-2 runs and backfilled pre-rename rows keep
        # the branch phase 1 pushed); new jobs get the configured prefix,
        # persisted before first use
        branch = (row.get("branch") or "").strip() \
            or f"{self.settings.branch_prefix}/sentry-{issue_id}"
        if branch != (row.get("branch") or ""):
            self.store.set_fields(issue_id, branch=branch)

        # live observation: v1 runs stream into the same broker the inbox detail
        # pane subscribes to (session/stream), exactly like feature stages —
        # and every event tees into the run transcript (§13) for replay
        broker = self.engine.stage_broker
        broker.start(issue_id)
        t_writer = transcripts.open_writer(
            self.settings, issue_id, f"v1-p{phase}-{int(time.time())}",
            {"kind": "v1", "phase": phase, "source": "sentry"})

        def emit(event, data):
            t_writer.write(event, data)
            broker.publish(issue_id, event, data)

        result = None
        try:
            # publish BEFORE the lock wait: the pane is already live (status is
            # running), and silence while another run holds the repo reads as a
            # hang. Moving start() inside the lock would be worse — a subscriber
            # with no turn gets an immediate 'done' and reads "run finished".
            emit("status",
                 "waiting for the repository workspace (another run may be using it)")
            pname, brief = self._job_context(row)
            async with self.locks.for_repo(target.repo):  # workspace toucher
                emit("status", "preparing the repository workspace")
                # workspace FIRST: the prompt's memory-write paths resolve the
                # repo's engine namespace from the actual clone, so legacy
                # `.gumo/` repos keep feeding memory (ENGINE.md §4)
                if phase == 2:
                    workspace = await prepare_workspace(self.settings, target, branch, keep_branch=True)
                    prompt = build_phase2_prompt(
                        target=target, branch=branch, issue=issue_info, stacktrace=stacktrace,
                        clickup_task_id=task_id,
                        analysis=row.get("analysis") or "(analysis missing)",
                        guidance=row.get("guidance") or "(no guidance recorded)",
                        product_name=pname, business_context=brief,
                        ns=engine_dir(workspace),
                    )
                else:
                    workspace = await prepare_workspace(self.settings, target, branch)
                    prompt = build_fix_prompt(
                        target=target, branch=branch, issue=issue_info, stacktrace=stacktrace,
                        clickup_task_id=task_id,
                        product_name=pname, business_context=brief,
                        ns=engine_dir(workspace),
                    )

                log.info("running claude for issue %s phase %s (%s)", issue_id, phase, target.repo)
                result = await run_claude(
                    self.settings, target, workspace, prompt, on_event=emit)
        finally:
            broker.finish(issue_id)
            t_writer.close(result.status if result else "exception")
        log.info("issue %s -> %s %s", issue_id, result.status, result.pr_url or "")
        await self.engine.record_prs(issue_id, result.pr_urls)

        if result.status == "needs_input":
            await self._park_awaiting(issue_id, task_id or "", result.detail)
            return
        if await self._maybe_auto_retry_v1(row, result.status, result.detail, task_id or ""):
            return

        self.store.set_status(issue_id, result.status, pr_url=result.pr_url, detail=result.detail)
        await self.clickup.set_status(task_id or "", result.status)

        if result.status == "pr_opened":
            await self.clickup.comment(task_id or "", f"Draft PR opened: {result.pr_url}")
            await self.sentry.post_comment(
                issue_id,
                f"{ENGINE_NAME} opened a draft PR for this issue: {result.pr_url}"
                + (f" (tracking: {row.get('clickup_task_url')})" if row.get("clickup_task_url") else ""),
            )
        elif result.status == "no_fix":
            analysis = result.detail.split("NO_FIX:", 1)[-1].strip()[:1500]
            await self.clickup.comment(task_id or "", f"No PR opened.\n\n{analysis}")
            await self.sentry.post_comment(
                issue_id, f"{ENGINE_NAME} investigated but did not open a PR:\n\n{analysis}"
            )
        else:  # error / timeout
            await self.clickup.comment(
                task_id or "", f"Run ended with status `{result.status}`: {result.detail[:500]}"
            )

    # ---------- task flow (v1) ----------

    async def _process_task(self, row: dict):
        """Manually reported request: phase 1 analysis (always parks for approval),
        phase 2 implementation after a human answers."""
        job_id = row["issue_id"]
        phase = int(row.get("phase") or 1)
        project = row.get("project") or ""

        target = self.settings.repo_for_project(project)
        if target is None:
            self.store.set_status(job_id, "skipped", detail=f"no repo mapped for '{project}'")
            return

        task_id = row.get("clickup_task_id") or ""
        self.store.set_fields(job_id, run_started_at=time.time())
        self.store.set_status(job_id, "running")
        await self.clickup.set_status(task_id, "running")

        task_info = {
            "id": job_id,
            "title": row.get("title") or "untitled request",
            "url": row.get("clickup_task_url") or "",
            "project": project,
        }
        request_text = row.get("request") or row.get("title") or ""
        # stored branch wins (phase 2 / backfilled rows); else configured prefix
        branch = (row.get("branch") or "").strip() \
            or f"{self.settings.branch_prefix}/{job_id}"
        if branch != (row.get("branch") or ""):
            self.store.set_fields(job_id, branch=branch)

        # live observation: stream this run to the inbox detail pane (see
        # _process_sentry for the same wiring on the sentry path), teeing every
        # event into the run transcript (§13)
        broker = self.engine.stage_broker
        broker.start(job_id)
        t_writer = transcripts.open_writer(
            self.settings, job_id, f"v1-p{phase}-{int(time.time())}",
            {"kind": "v1", "phase": phase, "source": "task"})

        def emit(event, data):
            t_writer.write(event, data)
            broker.publish(job_id, event, data)

        result = None
        try:
            pname, brief = self._job_context(row)
            emit("status", "preparing the repository workspace")
            # workspace FIRST — see _process_sentry: prompt memory paths follow
            # the clone's actual engine namespace
            if phase == 2:
                workspace = await prepare_workspace(self.settings, target, branch, keep_branch=True)
                prompt = build_task_implement_prompt(
                    target=target, branch=branch, task=task_info, request=request_text,
                    clickup_task_id=task_id or None,
                    analysis=row.get("analysis") or "(analysis missing)",
                    guidance=row.get("guidance") or "(no guidance recorded)",
                    product_name=pname, business_context=brief,
                    ns=engine_dir(workspace),
                )
            else:
                workspace = await prepare_workspace(self.settings, target, branch)
                prompt = build_task_plan_prompt(
                    target=target, branch=branch, task=task_info, request=request_text,
                    clickup_task_id=task_id or None,
                    product_name=pname, business_context=brief,
                )

            log.info("running claude for request %s phase %s (%s)", job_id, phase, target.repo)
            result = await run_claude(
                self.settings, target, workspace, prompt, on_event=emit)
        finally:
            broker.finish(job_id)
            t_writer.close(result.status if result else "exception")
        log.info("request %s -> %s %s", job_id, result.status, result.pr_url or "")
        await self.engine.record_prs(job_id, result.pr_urls)

        if result.status == "needs_input":
            await self._park_awaiting(job_id, task_id, result.detail)
            return
        if await self._maybe_auto_retry_v1(row, result.status, result.detail, task_id):
            return

        self.store.set_status(job_id, result.status, pr_url=result.pr_url, detail=result.detail)
        await self.clickup.set_status(task_id, result.status)

        if result.status == "pr_opened":
            await self.clickup.comment(task_id, f"Draft PR opened: {result.pr_url}")
        elif result.status == "no_fix":
            analysis = result.detail.split("NO_FIX:", 1)[-1].strip()[:1500]
            await self.clickup.comment(task_id, f"No PR opened.\n\n{analysis}")
        else:  # error / timeout
            await self.clickup.comment(
                task_id, f"Run ended with status `{result.status}`: {result.detail[:500]}"
            )

    async def _maybe_auto_retry_v1(self, row: dict, result_status: str,
                                   detail: str, task_id: str) -> bool:
        """ONE automatic requeue for a v1 run that died on an upstream hiccup
        (API 5xx, overloaded…) — mirrors the feature-stage policy in
        engine._after_run. Timeouts and run-produced errors stay manual.
        Returns True when the retry was armed (caller stops terminal handling)."""
        if result_status != "error" or not TRANSIENT_ERROR_RE.search(detail or ""):
            return False
        if int(row.get("auto_retries") or 0) >= 1:
            return False
        job_id = row["issue_id"]
        self.store.set_fields(job_id, auto_retries=1)
        self.store.set_status(job_id, "queued",
                              detail=f"transient upstream error — retrying automatically: "
                                     f"{(detail or '')[:300]}")
        self._enqueue(job_id, self._priority_for(row))
        await self.clickup.comment(
            task_id or "",
            f"{GATE_PREFIX} ♻️ the run hit a transient upstream error — retrying "
            "automatically (1/1).")
        log.info("auto-retry armed for %s after transient error", job_id)
        return True

    # ---------- HITL: parking and answering ----------

    async def _park_awaiting(self, job_id: str, task_id: str, analysis: str):
        """Park a task/sentry job. Crash-safe ordering: DB transition (with the
        pre-comment marker) commits BEFORE the ClickUp comment is posted."""
        comments = await self.clickup.comments(task_id)
        marker = comments[-1]["id"] if comments else ""
        # parking is a successful phase end: restore the transient-retry budget
        self.store.set_fields(job_id, analysis=analysis, auto_retries=0,
                              question=extract_questions(analysis), comment_marker=marker)
        self.store.set_status(job_id, "awaiting_input", detail=analysis[:2000])
        await self.clickup.comment(
            task_id,
            "**Human input needed before I change anything.**\n\n"
            f"{analysis}\n\n---\n"
            "Reply here with `/proceed <your decision/guidance>` to continue, or `/skip` "
            f"to drop this — or answer directly on the {ENGINE_NAME} dashboard.",
        )
        await self.clickup.set_status(task_id, "awaiting_input")
        job = self.store.get(job_id)
        if self.workspaces and job:
            await self.workspaces.notify_gate(
                job, f"\u23f8\ufe0f {job.get('title') or job_id} — waiting for your decision "
                     "before any code changes.")

    def request_steer(self, job_id: str, note: str, via: str = "dashboard") -> str:
        """Live mid-run course-correction from the session page. Delegates to the
        engine, which interrupts the running stage when it can (session persistence
        on) or records the note as guidance for the next checkpoint otherwise.
        Returns 'interrupting' | 'queued' | 'empty'."""
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if (job.get("kind") or "sentry") != "feature":
            raise ValueError("steering is only valid for feature pipelines")
        return self.engine.request_steer(job_id, note, via=via)

    async def answer_job(self, job_id: str, action: str, text: str, via: str,
                         actor: dict | None = None, override: bool = False) -> str:
        """Single resolution path for gate answers from BOTH channels.
        Returns the new status. Raises KeyError (unknown), ValueError (invalid
        action/state), GateConflict (lost the CAS race), GateForbidden (the
        actor does not own a role-exclusive gate — Epic A3). `actor` is the
        acting user row when resolvable (dashboard: always; ClickUp: via the
        clickup_user_id mapping); `override` is the audited dashboard-only
        admin bypass."""
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        kind = job.get("kind") or "sentry"
        if kind == "feature":
            return await self._answer_feature(job, action, text, via,
                                              actor=actor, override=override)
        if kind == "watch":
            # BEFORE the v1 redo-refusal: the Iterate gate supports /redo <days>
            return await self._answer_watch(job, action, text, via,
                                            actor=actor, override=override)
        if action == "redo":
            raise ValueError(f"redo is only valid for feature pipelines, not kind '{kind}'")
        return await self._answer_v1(job, action, text, via)

    async def _answer_v1(self, job: dict, action: str, text: str, via: str) -> str:
        job_id = job["issue_id"]
        if job["status"] != "awaiting_input":
            raise ValueError(f"job is '{job['status']}', not awaiting_input")
        task_id = job.get("clickup_task_id") or ""

        # via is the channel, optionally with the acting user: "dashboard:manish"
        from_dashboard = via.startswith("dashboard")

        if action == "skip":
            if not self.store.cas_status(job_id, ["awaiting_input"], "skipped",
                                         question="", detail=f"skipped by human via {via}"):
                raise GateConflict("already answered")
            self.store.guidance_add(job_id, None, "skip", text, via)
            if from_dashboard:
                await self.clickup.comment(task_id, f"{GATE_PREFIX} Decision (via {via}): skip"
                                                    + (f" — {text}" if text else ""))
            await self.clickup.set_status(task_id, "skipped")
            return "skipped"

        guidance = text or "Proceed as you proposed."
        if not self.store.cas_status(job_id, ["awaiting_input"], "queued",
                                     guidance=guidance, phase=2, question=""):
            raise GateConflict("already answered")
        self.store.guidance_add(job_id, None, "proceed", guidance, via)
        if from_dashboard:
            await self.clickup.comment(task_id, f"{GATE_PREFIX} Decision (via {via}): proceed — {guidance}")
        else:
            await self.clickup.comment(task_id, "Got it — proceeding with the fix now.")
        self._enqueue(job_id, PRIO_HUMAN)
        log.info("job %s advanced to phase 2 via %s", job_id, via)
        return "queued"

    async def _sync_decision_field(self, task_id: str, stage: int, label: str, text: str):
        """Mirror substantive gate answers into the ticket's `Decisions` field —
        the original workflow's Grounding contract (append, never overwrite)."""
        if not (text and task_id and self.settings.clickup_field_sync_enabled):
            return
        await self.clickup.field_append(task_id, "Decisions",
                                        f"P{stage}{label}: {text[:400]}")

    def _register_decision(self, job: dict, stage: int | None, title: str,
                           text: str, via: str, *, gid: int,
                           scope: str = "job", job_id: str | None = None):
        """Epic D2: auto-register a SUBSTANTIVE gate answer (non-empty text —
        the same emptiness guard as _sync_decision_field) into the decision
        registry, ref='g<guidance id>' so any replay dedupes. Best-effort:
        a registry hiccup must never break a won gate transition."""
        if not (text or "").strip():
            return
        links = [job["clickup_task_url"]] if job.get("clickup_task_url") else []
        try:
            self.store.decision_add(
                "gate", text.strip(), ref=f"g{gid}", scope=scope,
                job_id=job_id if job_id is not None else job["issue_id"],
                workspace_id=job.get("workspace_id"),
                project=job.get("project") or "", stage=stage,
                title=title, decided_by=via, links=links)
        except Exception:
            log.exception("decision auto-registration failed for %s (non-fatal)",
                          job.get("issue_id"))

    def _owner_guard(self, job: dict, actor: dict | None,
                     override: bool) -> tuple["roles.GateOwner | None", bool]:
        """Role-exclusive enforcement shared by feature gates AND the watch
        Iterate gate (Epic A3 / ENGINE.md §2b): raises GateForbidden for a
        non-owner, or returns (owner, admin_override) — True only for the
        explicit, dashboard-only admin bypass (audited by the caller AFTER a
        won CAS). Inert when the job records no explicit DRIs."""
        owner = roles.gate_owner(self.store, self.settings, self._ws_row(job), job)
        admin_override = False
        if owner is not None and owner.enforce and not roles.actor_is_owner(owner, actor):
            if actor and actor.get("role") == "admin" and override:
                admin_override = True  # dashboard-only, explicit, audited by caller
            else:
                raise GateForbidden(f"this is a {owner.role} gate, owned by {owner.display}")
        return owner, admin_override

    async def _answer_feature(self, job: dict, action: str, text: str, via: str,
                              actor: dict | None = None, override: bool = False) -> str:
        """Role-exclusive gates (Epic A3), enforced at the single choke point
        BOTH channels funnel through — before any CAS, never replacing it.
        Inert when the job records no explicit DRIs (gate_owner enforce=False
        or None — solo installs and pre-upgrade jobs behave exactly as today).
        Applies to proceed/redo/skip including ask-gates and redo-from-error;
        gate chat and plain comments are untouched."""
        owner, admin_override = self._owner_guard(job, actor, override)
        result = await self._answer_feature_inner(job, action, text, via)
        if admin_override:
            # recorded only AFTER the transition succeeded — a lost CAS raises
            # GateConflict above and must leave NO override audit row. The ref
            # is a uuid: an audit record must never be eaten by the dedupe key.
            self.store.gate_event_add(
                job["issue_id"], "admin_override", ref=uuid.uuid4().hex,
                stage=int(job.get("stage") or 0), actor=via,
                detail=f"{action} on a {owner.role} gate owned by {owner.display}")
            log.info("admin override: %s %s by %s", job["issue_id"], action, via)
        return result

    async def _answer_feature_inner(self, job: dict, action: str, text: str, via: str) -> str:
        job_id = job["issue_id"]
        stage = int(job.get("stage") or 0)
        task_id = job.get("clickup_task_id") or ""

        if action == "skip":
            if not self.store.cas_status(job_id, ["awaiting_input", "error", "timeout"],
                                         "skipped", expected_stage=stage,
                                         question="", detail=f"pipeline aborted by human via {via}"):
                raise GateConflict("already answered")
            self.store.guidance_add(job_id, stage, "skip", text, via, job.get("parked_head") or "")
            self.store.stage_run_gate_answered(job_id, stage, "skip")
            await self.clickup.comment(task_id, f"{GATE_PREFIX} Pipeline aborted at P{stage} (via {via})."
                                                " The branch is left intact.")
            await self.clickup.set_status(task_id, "skipped")
            return "skipped"

        if action == "redo":
            target_stage = stage
            m = REDO_TARGET_RE.match(text or "")
            if m:
                requested = int(m.group(1))
                if requested > stage:
                    raise ValueError(f"cannot redo P{requested}: pipeline is only at P{stage}")
                target_stage = requested
                text = text[m.end():].strip()
            # a redo at an ask-gate discards the pending resume: fresh restart
            if not self.store.cas_status(job_id, ["awaiting_input", "error", "timeout"],
                                         "queued", expected_stage=stage,
                                         stage=target_stage, question="",
                                         pending_redo_stage=target_stage,
                                         gate_kind="", resume_session_id="",
                                         resume_stage=None, resume_attempt=None,
                                         resume_head="", resume_answer="", ask_count=0,
                                         auto_retries=0):
                raise GateConflict("already answered")
            gid = self.store.guidance_add(job_id, target_stage, "redo", text, via,
                                          job.get("parked_head") or "")
            self._register_decision(job, target_stage, f"P{target_stage} redo",
                                    text, via, gid=gid)
            self.store.stage_run_gate_answered(job_id, stage, "redo")
            await self._sync_decision_field(task_id, target_stage, " (redo)", text)
            if (self.settings.clickup_field_sync_enabled and text and task_id
                    and self.settings.clickup_friction_field):
                # a human redo IS workflow friction — feed the improvement loop
                await self.clickup.field_append(
                    task_id, self.settings.clickup_friction_field,
                    f"P{target_stage} (human) · redo requested · {text[:300]}")
            if target_stage < stage:
                await self._mark_superseded(job, target_stage)
            await self.clickup.comment(
                task_id, f"{GATE_PREFIX} Redoing P{target_stage} (answered via {via})."
                         + (f" Corrections: {text[:500]}" if text else ""))
            self._enqueue(job_id, PRIO_HUMAN)
            return "queued"

        if action != "proceed":
            raise ValueError(f"unknown action '{action}'")
        if job["status"] != "awaiting_input":
            raise ValueError(f"job is '{job['status']}', not awaiting_input")

        # STAGE_ASK gate: 'proceed' is the ANSWER — a distinct transition that
        # keeps the stage and attempt so the session resumes in place. It
        # explicitly bypasses the P9 terminal branch (asks never occur at P9).
        if (job.get("gate_kind") or "") == "ask":
            answer = text or "Proceed as you suggested."
            if not self.store.cas_status(job_id, ["awaiting_input"], "queued",
                                         expected_stage=stage,
                                         question="", resume_answer=answer):
                raise GateConflict("already answered")
            gid = self.store.guidance_add(job_id, stage, "answer", answer, via,
                                          job.get("parked_head") or "")
            self._register_decision(job, stage, f"P{stage} answer", text, via, gid=gid)
            self.store.stage_run_gate_answered(job_id, stage, "answer")
            self._distill_chat(job, stage)
            await self._sync_decision_field(task_id, stage, " (ask)", text)
            await self.clickup.comment(
                task_id, f"{GATE_PREFIX} Answer received (via {via}) — resuming P{stage} "
                         "where it stopped.")
            self._enqueue(job_id, PRIO_HUMAN)
            log.info("feature %s ask answered, resuming P%s via %s", job_id, stage, via)
            return "queued"

        guidance = text or "Approved — continue."
        if stage >= 9:
            final = "pr_opened" if job.get("pr_url") else "no_fix"
            if not self.store.cas_status(job_id, ["awaiting_input"], final,
                                         expected_stage=stage, question="",
                                         detail="pipeline complete — P9 approved"):
                raise GateConflict("already answered")
            gid = self.store.guidance_add(job_id, stage, "proceed", guidance, via,
                                          job.get("parked_head") or "")
            self._register_decision(job, stage, f"P{stage} proceed", text, via, gid=gid)
            self.store.stage_run_gate_answered(job_id, stage, "proceed")
            await self._sync_decision_field(task_id, stage, "", text)
            await self.clickup.comment(
                task_id, f"{GATE_PREFIX} P9 approved (via {via}) — pipeline complete. "
                         f"{'PR ready to un-draft: ' + job['pr_url'] if job.get('pr_url') else ''}")
            await self.clickup.set_status(task_id, final)
            # conveyor mirror: the shipped feature sits in Dogfood until its PR
            # merges (the shepherd then slides it to Complete)
            await self.engine.sync_stage_field(job, "shipped")
            # outcome loop (Epic B4), early-merge case: a human merged the PR
            # before P9 approval — the shepherd's merged branch fired while the
            # pipeline was live and the spawn guard refused. Now the feature is
            # terminal: spawn if a tracked PR is already merged.
            if final == "pr_opened" and any(
                    (p.get("state") or "") == "merged"
                    for p in self.store.prs_for(job_id)):
                await self._maybe_spawn_watch(self.store.get(job_id) or job)
            return final

        if not self.store.cas_status(job_id, ["awaiting_input"], "queued",
                                     expected_stage=stage,
                                     stage=stage + 1, stage_attempts=0, question="",
                                     ask_count=0):
            raise GateConflict("already answered")
        gid = self.store.guidance_add(job_id, stage, "proceed", guidance, via,
                                      job.get("parked_head") or "")
        self._register_decision(job, stage, f"P{stage} proceed", text, via, gid=gid)
        self.store.stage_run_gate_answered(job_id, stage, "proceed")
        self._distill_chat(job, stage)
        await self._sync_decision_field(task_id, stage, "", text)
        await self.clickup.comment(
            task_id, f"{GATE_PREFIX} P{stage} approved (via {via}) — running P{stage + 1} next.")
        self._enqueue(job_id, PRIO_HUMAN)
        log.info("feature %s advanced to P%s via %s", job_id, stage + 1, via)
        return "queued"

    def _distill_chat(self, job: dict, stage: int):
        """A gate conversation must outlive its gate (docs/CONVERSATIONS.md §4):
        record the last engine answer as guidance so later stages and P9's ADR
        pass see what the clarification concluded."""
        turns = self.store.chat_for(job["issue_id"], stage)
        last_engine = next((t for t in reversed(turns)
                            if t["role"] == "engine" and not t.get("degraded")), None)
        if last_engine:
            self.store.guidance_add(job["issue_id"], stage, "chat",
                                    (last_engine["text"] or "")[:1500], "engine",
                                    job.get("parked_head") or "")

    async def _mark_superseded(self, job: dict, target_stage: int):
        """Redo of an earlier stage: banner downstream artifact mirrors so humans
        don't edit documents that are about to be regenerated."""
        job_id = job["issue_id"]
        for state in self.store.artifacts_for(job_id):
            m = re.match(r"^P(\d)-", state["artifact"])
            if not m or int(m.group(1)) <= target_stage or not state["subtask_id"]:
                continue
            task = await self.clickup.get_task(state["subtask_id"])
            if not task or task.get("missing"):
                continue
            desc = task.get("description") or ""
            if desc.startswith("**SUPERSEDED"):
                continue
            banner = (f"**SUPERSEDED by redo of P{target_stage} — this document will be "
                      "regenerated; edits here will be ignored.**\n\n")
            await self.clickup.update_description(state["subtask_id"], banner + desc)
            readback = await self.clickup.get_task(state["subtask_id"])
            if readback and not readback.get("missing"):
                from .artifacts import semantic_hash
                self.store.artifact_set(job_id, state["artifact"],
                                        synced_hash=semantic_hash(readback.get("description") or ""),
                                        flags="superseded")

    def _ticket_description(self, issue: dict, row: dict) -> str:
        return (
            f"**Sentry issue:** {issue.get('permalink', '')}\n"
            f"**Project:** {(issue.get('project') or {}).get('slug', '?')} | "
            f"**Level:** {issue.get('level', '?')} | "
            f"**Events:** {issue.get('count', '?')} | "
            f"**Users:** {issue.get('userCount', '?')}\n"
            f"**Source:** {row.get('source', 'webhook')} | "
            f"**Grade:** {row.get('grade_reasons') or 'n/a'}\n\n"
            f"{issue.get('culprit', '')}\n\n"
            f"_Automated fix attempt by {ENGINE_NAME}. Claude posts progress below. "
            "If it asks for input, reply `/proceed <guidance>` or `/skip`._"
        )

    # ---------- the outcome loop (Epic B4/B5) ----------

    WATCH_REDO_RE = re.compile(r"^\s*(\d{1,4})\b\s*")

    def _analytics_provider_for(self, job: dict):
        """The job's analytics driver. Guards the bare-Worker shape (tests
        construct Worker without the workspace service) — NullAnalytics-or-
        instance-env instead of an AttributeError."""
        if self.workspaces:
            return self.workspaces.analytics_for(self.workspaces.for_job(job))
        return analytics.provider_for(self.settings, None)

    async def _maybe_spawn_watch(self, job: dict):
        """Spawn the post-ship watch for a merged feature. Idempotent by the
        `watch-<feature id>` row; fires ONLY when the feature pipeline is
        terminal at 'pr_opened' — a PR merged mid-pipeline must never put a
        second gate on the same ticket while feature gates are still live
        (the P9-approval path re-checks and spawns then)."""
        if not self.settings.watch_enabled:
            return
        if (job.get("kind") or "") != "feature":
            return
        if (job.get("status") or "") != "pr_opened":
            return
        job_id = job["issue_id"]
        watch_id = f"watch-{job_id}"
        if self.store.get(watch_id):
            return  # already spawned (this lap; re-intake resets the row)
        metric = (job.get("success_metric") or "").strip()
        event = (job.get("metric_event") or "").strip()
        task_id = job.get("clickup_task_id") or ""
        if not metric and not event:
            # one note per FEATURE, not per merged PR (multi-PR features hit
            # the shepherd's merged branch once per PR) — gate_events dedupe
            if self.store.gate_event_add(
                    job_id, "watch_skipped", ref="no-metric", actor="engine",
                    detail="no success metric recorded — outcome watch skipped"):
                await self.clickup.comment(
                    task_id, f"{GATE_PREFIX} no success metric recorded — outcome "
                             "watch skipped. Add a metric at intake (or a P9 "
                             "SUCCESS_METRIC/METRIC_EVENT line) next time.")
            return
        days = job.get("metric_window_days") or self.settings.metric_window_days_default
        days = max(1, min(365, int(days)))
        now = time.time()
        self.store.watch_insert(
            watch_id,
            title=f"watch: {job.get('title') or job_id}"[:300],
            project=job.get("project") or "",
            workspace_id=job.get("workspace_id"),
            related_jobs=job_id,
            success_metric=metric,
            metric_target=(job.get("metric_target") or "").strip(),
            metric_event=event,
            metric_window_days=days,
            watch_started_at=now,
            watch_deadline=now + days * 86400,
            # founder-owned Iterate gate: BOTH DRI columns are copied so
            # roles.gate_owner enforces it exactly like feature gates
            # (founder slot wins, dev is the fallback owner; legacy `owner`
            # stays display/assignment only)
            owner=(job.get("founder_dri") or "").strip() or (job.get("owner") or "").strip(),
            founder_dri=(job.get("founder_dri") or "").strip(),
            dev_dri=(job.get("dev_dri") or "").strip(),
            clickup_task_id=task_id,
            clickup_task_url=job.get("clickup_task_url") or "",
            cu_list_id=job.get("cu_list_id") or "",
        )
        # deliberately NO clickup.set_status here: the shepherd just slid the
        # Stage field to Complete — the ticket status flips only at park/close
        await self.clickup.comment(
            task_id, f"{GATE_PREFIX} 📈 outcome watch started: "
                     f"'{metric or event}' for {days} day(s). The Iterate gate "
                     "parks here when the window closes.")
        log.info("watch %s spawned for %s (%d days)", watch_id, job_id, days)

    async def watch_forever(self):
        """The post-ship watch loop: pure HTTP metric reads (no repo locks, no
        Claude tokens), a verdict + founder-owned Iterate gate at deadline."""
        while True:
            await asyncio.sleep(self.settings.watch_interval_seconds)
            try:
                await self._watch_pass()
            except Exception:
                log.exception("watch pass failed")

    async def _watch_pass(self):
        if not self.settings.watch_enabled:
            return
        now = time.time()
        for job in self.store.by_status(["watching"]):
            if (job.get("kind") or "") != "watch":
                continue
            try:
                provider = self._analytics_provider_for(job)
                if now >= float(job.get("watch_deadline") or 0):
                    await self._finish_watch(job, provider)
                elif self.store.reading_last_at(job["issue_id"]) < now - WATCH_READ_THROTTLE_SECONDS:
                    await self._watch_read(job, provider, now)
            except Exception:
                log.exception("watch %s pass failed", job["issue_id"])

    async def _watch_read(self, job: dict, provider, now: float):
        """One daily window-to-date read. Failures leave a visible detail on
        the job — never a fake reading row."""
        job_id = job["issue_id"]
        started = float(job.get("watch_started_at") or now)
        window = int(job.get("metric_window_days") or self.settings.metric_window_days_default)
        day = min(window, int((now - started) // 86400) + 1)
        metric = job.get("success_metric") or ""
        event = job.get("metric_event") or ""
        res = await provider.query_metric(metric, day, event=event)
        if res.get("status") == "ok":
            self.store.reading_add(job_id, metric, event, observed=res.get("total"),
                                   window_day=day, detail=(res.get("detail") or "")[:400],
                                   window_start=started)
            self.store.set_fields(job_id, detail=f"day {day}/{window}: "
                                                 f"window-to-date {res.get('total')}")
        else:
            self.store.set_fields(
                job_id, detail=f"metric read {res.get('status')}: "
                               f"{(res.get('detail') or '')[:300]}")

    async def _finish_watch(self, job: dict, provider):
        """Window closed: final read, verdict, ledger row, Iterate-gate park.
        Ordering contract: the outcomes row and the CAS to awaiting_input
        commit BEFORE any ClickUp call (all of which are best-effort)."""
        job_id = job["issue_id"]
        started = float(job.get("watch_started_at") or 0)
        window = int(job.get("metric_window_days") or self.settings.metric_window_days_default)
        metric = job.get("success_metric") or ""
        event = job.get("metric_event") or ""
        final = await provider.query_metric(metric, window, event=event)
        if final.get("status") == "ok":
            self.store.reading_add(job_id, metric, event, observed=final.get("total"),
                                   window_day=window, detail="final read",
                                   window_start=started)
        existing = self.store.outcome_for(job_id)
        if existing and existing.get("baseline") is not None:
            # a /redo re-finish keeps the ORIGINAL pre-merge baseline — a
            # window ending at the refreshed start would include post-ship data
            baseline = existing["baseline"]
        else:
            # no persisted baseline (first finish, or the first finish's query
            # failed and stored NULL): the query must END at the ORIGINAL
            # merge-time spawn, never the current window's start — a /redo
            # overwrites watch_started_at, and a window ending there would be
            # entirely post-ship data mislabeled "same-length pre-ship window"
            # (ENGINE.md §2b). The spawn instant survives as the row's
            # created_at (watch_insert stamps it in the same transaction).
            anchor = float(job.get("created_at") or started)
            base_res = await provider.query_metric(metric, window, event=event, end=anchor)
            baseline = base_res.get("total") if base_res.get("status") == "ok" else None
        readings = self.store.readings_for(job_id, window_start=started)
        verdict, inputs = outcome.compute_verdict(
            readings, job.get("metric_target") or "", baseline,
            self.settings.outcome_flat_band_pct)
        feature_id = (job.get("related_jobs") or "").split(",")[0].strip()
        self.store.outcome_add(
            job_id, feature_id, job.get("workspace_id"),
            metric=metric, metric_event=event,
            target=(job.get("metric_target") or "").strip(),
            observed=inputs.get("observed"), baseline=baseline, window_days=window,
            verdict=verdict, verdict_inputs=json.dumps(inputs))
        fields = self.store.outcome_for(job_id) or {}
        packet = outcome.build_gate_packet(job, fields, readings)
        task_id = job.get("clickup_task_id") or ""
        comments = await self.clickup.comments(task_id)
        marker = comments[-1]["id"] if comments else ""
        if not self.store.cas_status(job_id, ["watching"], "awaiting_input",
                                     analysis=packet,
                                     question=extract_questions(packet),
                                     comment_marker=marker,
                                     detail=packet[:2000]):
            return  # a human raced the finish (e.g. /skip) — theirs wins, silently
        # everything below is best-effort visibility AFTER the committed CAS
        await self.clickup.comment(
            task_id,
            f"{GATE_PREFIX} **Iterate gate: {job.get('title') or job_id} — "
            f"verdict {verdict}**\n\n{packet[:6000]}\n\n---\n"
            "Reply `/proceed <learning>` to log the learning and close, "
            "`/redo <days>` to watch again, or `/skip` to close without a "
            "learning — here or on the dashboard.")
        await self.clickup.set_status(task_id, "awaiting_input")
        owner = (job.get("owner") or "").strip()
        if owner:
            await self.clickup.set_assignee(task_id, owner)
        if self.workspaces:
            await self.workspaces.notify_gate(
                job, f"📊 {job.get('title') or job_id} — outcome verdict "
                     f"'{verdict}'; the Iterate gate is waiting.")
        log.info("watch %s parked at the Iterate gate (verdict %s)", job_id, verdict)

    async def _answer_watch(self, job: dict, action: str, text: str, via: str,
                            actor: dict | None = None, override: bool = False) -> str:
        """The Iterate gate's verbs. Founder-owned and role-enforced exactly
        like feature gates (ENGINE.md §2b — gate_owner handles kind='watch');
        inert without explicit DRIs on the row. Every mutation is CAS-guarded
        and lands in guidance_log (auditable); ClickUp strictly after the CAS,
        best-effort."""
        owner, admin_override = self._owner_guard(job, actor, override)
        result = await self._answer_watch_inner(job, action, text, via)
        if admin_override:
            # recorded only AFTER the transition succeeded (a lost CAS raises
            # GateConflict and must leave NO override row); uuid ref so the
            # dedupe key can never eat an audit record — same discipline as
            # the feature-gate override
            self.store.gate_event_add(
                job["issue_id"], "admin_override", ref=uuid.uuid4().hex,
                stage=None, actor=via,
                detail=f"{action} on the {owner.role}-owned Iterate gate "
                       f"owned by {owner.display}")
            log.info("admin override: %s %s by %s", job["issue_id"], action, via)
        return result

    async def _answer_watch_inner(self, job: dict, action: str, text: str, via: str) -> str:
        job_id = job["issue_id"]
        task_id = job.get("clickup_task_id") or ""
        now = time.time()
        text = (text or "").strip()

        if action == "skip":
            # also valid mid-window: cancelling a watch is a human decision
            if not self.store.cas_status(job_id, ["awaiting_input", "watching"], "skipped",
                                         question="",
                                         detail=f"watch closed by human via {via}"):
                raise GateConflict("already answered")
            if self.store.outcome_for(job_id):
                self.store.outcome_set(job_id, decided_by=via, decided_at=now)
            self.store.guidance_add(job_id, None, "skip", text, via)
            await self.clickup.comment(
                task_id, f"{GATE_PREFIX} Outcome watch closed (via {via}) — the "
                         "verdict stands; no learning recorded.")
            await self.clickup.set_status(task_id, "skipped")
            return "skipped"

        if action == "redo":
            days = int(job.get("metric_window_days")
                       or self.settings.metric_window_days_default)
            m = self.WATCH_REDO_RE.match(text)
            if m:
                requested = int(m.group(1))
                if not 1 <= requested <= 365:
                    raise ValueError(f"watch window must be 1–365 days, got {requested}")
                days = requested
                text = text[m.end():].strip()
            if not self.store.cas_status(job_id, ["awaiting_input"], "watching",
                                         question="", metric_window_days=days,
                                         watch_started_at=now,
                                         watch_deadline=now + days * 86400,
                                         detail=f"watch re-armed for {days} day(s) via {via}"):
                raise GateConflict("already answered")
            self.store.guidance_add(job_id, None, "redo", text, via)
            await self.clickup.comment(
                task_id, f"{GATE_PREFIX} Watching again for {days} day(s) (via {via})."
                         + (f" Notes: {text[:300]}" if text else ""))
            return "watching"

        if action != "proceed":
            raise ValueError(f"unknown action '{action}'")
        if not self.store.cas_status(job_id, ["awaiting_input"], "done",
                                     question="", detail="outcome recorded"):
            raise GateConflict("already answered")
        self.store.outcome_set(job_id, learning=text, decided_by=via, decided_at=now)
        gid = self.store.guidance_add(job_id, None, "proceed", text, via)
        # Epic D2: the Iterate learning is the measured-reality decision the
        # next lap starts from — registered scope='product' against the
        # FEATURE id, ref='g<gid>' (the proceed guidance row) so any replay
        # dedupes instead of duplicating.
        feature_id = (job.get("related_jobs") or "").split(",")[0].strip()
        feature_title = (job.get("title") or job_id).removeprefix("watch: ")
        self._register_decision(job, None, f"Outcome: {feature_title}"[:200],
                                text, via, gid=gid, scope="product",
                                job_id=feature_id)
        await self.clickup.comment(
            task_id, f"{GATE_PREFIX} Outcome recorded (via {via})."
                     + (f" Learning: {text[:400]}" if text else ""))
        await self.clickup.set_status(task_id, "done")
        if self.settings.outcome_memory_prs:
            # background, strong-ref'd: the HTTP answer never blocks on a repo lock
            t = asyncio.create_task(self._outcome_memory_task(self.store.get(job_id) or job))
            self._bg_tasks.add(t)
            t.add_done_callback(self._bg_tasks.discard)
        return "done"

    async def _outcome_memory_task(self, job: dict):
        """Mechanical (NO model run) memory propagation: changelog entry (+ ADR
        when a learning was recorded) on a fresh branch, pushed, opened as a
        draft PR. Git stays truth — the entry only enters the memory tree via a
        human-merged PR; the outcomes DB row is the record either way. Never
        raises: failures land in the job detail + a ClickUp note."""
        job_id = job["issue_id"]
        try:
            feature_id = (job.get("related_jobs") or "").split(",")[0].strip()
            feature = self.store.get(feature_id) or {}
            row = self.store.outcome_for(job_id) or {}
            target = self.settings.repo_for_project(job.get("project") or "")
            if target is None:
                self.store.set_fields(job_id, detail="outcome memory PR skipped: "
                                                     "no repo mapped")
                return
            branch = f"{self.settings.branch_prefix}/outcome-{feature_id or job_id}"
            async with self.locks.for_repo(target.repo):
                workspace = await prepare_workspace(self.settings, target, branch)
                # the CLONE resolves the namespace (legacy `.gumo/` repos keep
                # their tree) — never the literal constant
                ns = engine_dir(workspace)
                rel, body = outcome.build_outcome_entry(row, feature, ns=ns)
                path = Path(workspace) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
                if (row.get("learning") or "").strip():
                    arel, abody = outcome.build_outcome_adr(row, feature, ns=ns)
                    apath = Path(workspace) / arel
                    apath.parent.mkdir(parents=True, exist_ok=True)
                    apath.write_text(abody)
                await git(workspace, "add", "-A")
                code, out = await git(workspace, "commit", "-m",
                                      f"outcome: {row.get('verdict') or 'recorded'} — "
                                      f"{feature_id or job_id}")
                if code != 0 and "nothing to commit" not in out:
                    raise RuntimeError(f"commit failed: {out[-300:]}")
                code, out = await git(workspace, "push", "-u", "origin", branch)
                if code != 0:
                    raise RuntimeError(f"push failed: {out[-300:]}")
            url = await self.engine.github.create_pr(
                target.repo, head=branch, base=target.base,
                title=f"outcome: {job.get('title') or feature_id}"[:200],
                body=f"Measured outcome for `{feature_id or job_id}` — verdict "
                     f"**{row.get('verdict') or 'unmeasured'}**.\n\n"
                     "Mechanical memory-propagation PR opened by the outcome loop; "
                     "review and merge to fold the result into product memory.",
                draft=True)
            if url:
                # doc draft: tracked, never review-bot kicked
                await self.engine.record_prs(job_id, [url], kickoff=False)
                await self.clickup.comment(
                    job.get("clickup_task_id") or "",
                    f"{GATE_PREFIX} 📝 outcome written into product memory — "
                    f"draft PR to review: {url}")
            else:
                self.store.set_fields(
                    job_id, detail=f"outcome memory: files pushed to {branch} but "
                                   "the draft PR could not be opened")
        except Exception as e:
            log.exception("outcome memory task failed for %s", job_id)
            try:
                self.store.set_fields(job_id,
                                      detail=f"outcome memory PR failed: {str(e)[:300]}")
                await self.clickup.comment(
                    job.get("clickup_task_id") or "",
                    f"{GATE_PREFIX} outcome memory PR failed (non-fatal — the "
                    f"ledger row is the record): {str(e)[:200]}")
            except Exception:
                pass

    # ---------- background loops ----------

    async def poll_clickup_forever(self):
        if not self.clickup.enabled:
            log.info("ClickUp disabled; HITL poller not started")
            return
        while True:
            await asyncio.sleep(self.settings.clickup_poll_seconds)
            try:
                await self._poll_awaiting()
            except Exception:
                log.exception("ClickUp poll iteration failed")
            try:
                await self._poll_intake()
            except Exception:
                log.exception("ClickUp intake scan failed")

    async def _poll_awaiting(self):
        # error/timeout features are included so a `/redo` re-kick works by comment too
        for job in self.store.by_status(["awaiting_input", "error", "timeout"]):
            is_feature = (job.get("kind") or "sentry") == "feature"
            if job["status"] != "awaiting_input" and not is_feature:
                continue
            task_id = job.get("clickup_task_id")
            if not task_id:
                continue
            handled = await self._scan_verbs(job, task_id, use_marker=True)
            if handled or not is_feature:
                continue
            # feature gates also accept verbs on any artifact subtask
            gate_posted = self._latest_gate_posted(job)
            for state in self.store.artifacts_for(job["issue_id"]):
                if state["subtask_id"]:
                    if await self._scan_verbs(job, state["subtask_id"], use_marker=False,
                                              after=gate_posted):
                        break

    def _latest_gate_posted(self, job: dict) -> float:
        # one implementation, shared with the inbox/SLA readers (JobStore owns
        # it so main.py never reaches into the worker)
        return self.store.latest_gate_posted(job["issue_id"], int(job.get("stage") or 0))

    async def _scan_verbs(self, job: dict, source_task_id: str, use_marker: bool,
                          after: float = 0.0) -> bool:
        """Scan one comment stream for gate verbs; route them through answer_job
        (CAS makes reprocessing harmless). Returns True if a verb was handled."""
        comments = await self.clickup.comments(source_task_id)
        marker = job.get("comment_marker") or ""
        if use_marker and not marker:
            # No marker (adopted ticket, or a crash before the first park set one):
            # NEVER replay the whole history — a months-old '/proceed' must not
            # auto-answer this gate. Fall back to a date fence at gate-post time.
            use_marker, after = False, self._latest_gate_posted(job)
        seen_marker = not use_marker
        for c in comments:
            if use_marker and not seen_marker:
                seen_marker = c["id"] == marker
                continue
            if not use_marker and c.get("date", 0) <= after:
                continue
            text = (c.get("text") or "").strip()
            lowered = text.lower()
            action = None
            payload = ""
            for verb in ("/proceed", "/redo", "/skip"):
                if lowered.startswith(verb):
                    action = verb[1:]
                    payload = text[len(verb):].strip()
                    break
            if action is None:
                # engine-authored comments (gate posts, chat mirrors) are inert
                if text.startswith(ENGINE_COMMENT_PREFIXES):
                    if use_marker:
                        self.store.set_fields(job["issue_id"], comment_marker=c["id"])
                    continue
                # a human replied conversationally — never drop it silently
                # (docs/CONVERSATIONS.md §2): nudge once per comment, keep scanning.
                # Watch (Iterate) gates included — a non-verb reply on a parked
                # verdict must not be re-scanned forever with no response.
                if (use_marker and job["status"] == "awaiting_input"
                        and (job.get("kind") or "") in ("feature", "watch") and text):
                    await self.clickup.comment(
                        job.get("clickup_task_id") or "",
                        f"{GATE_PREFIX} I only act on `/proceed`, `/redo` or `/skip` here — "
                        f"did you mean `/proceed {text[:120]}`? "
                        "(For back-and-forth questions, use the chat box on the dashboard.)",
                    )
                    self.store.set_fields(job["issue_id"], comment_marker=c["id"])
                continue
            # ---- answer attribution (Epic A1): who is issuing this verb? ----
            user_id = str(c.get("user_id") or "")
            actor = self.store.user_for_clickup_id(user_id)
            parent_task = job.get("clickup_task_id") or ""
            if actor is None and roles.attribution_required(
                    self.settings, self._ws_row(job), self.store):
                # refuse, idempotently per comment id: the UNIQUE(job, kind,
                # ref) row is what makes subtask-stream refusals (no marker)
                # reply exactly once. DB writes (event + marker) land BEFORE
                # the ClickUp reply, and the dedupe-hit path SKIPS the comment
                # (continue) so a refused comment can never wedge the scan —
                # the real owner's later verb on the same stream still runs.
                first = self.store.gate_event_add(
                    job["issue_id"], "refused_unattributed", ref=c["id"],
                    stage=job.get("stage"),
                    actor=f"clickup:{c.get('username') or '?'}#{user_id or '?'}")
                if use_marker:
                    self.store.set_fields(job["issue_id"], comment_marker=c["id"])
                if not first:
                    continue
                await self.clickup.comment(
                    parent_task,
                    f"{GATE_PREFIX} `{text[:80]}` from @{c.get('username') or 'unknown'} "
                    "was NOT applied — this ClickUp account isn't linked to a "
                    f"{ENGINE_NAME} user. An admin can link it in Settings → Users; "
                    "meanwhile link your ClickUp account or answer on the dashboard.")
                return True
            via = (f"clickup:{actor['username']}" if actor
                   else f"clickup:{c.get('username') or 'unknown'}#{user_id or '?'}")
            try:
                await self.answer_job(job["issue_id"], action, payload, via=via, actor=actor)
            except GateConflict:
                pass  # answered elsewhere — fine
            except GateForbidden as e:
                # role-exclusive gate (Epic A3): not this person's gate. Same
                # idempotence/ordering discipline as the attribution refusal;
                # ClickUp offers NO override path (fail closed) — an admin
                # overrides from the dashboard.
                first = self.store.gate_event_add(
                    job["issue_id"], "refused_wrong_role", ref=c["id"],
                    stage=job.get("stage"), actor=via, detail=str(e)[:300])
                if use_marker:
                    self.store.set_fields(job["issue_id"], comment_marker=c["id"])
                if not first:
                    continue
                await self.clickup.comment(
                    parent_task,
                    f"{GATE_PREFIX} Not applied: {e} — only they can `/proceed`/`/redo`/"
                    "`/skip` this gate. If this IS your gate, link your ClickUp account "
                    "(Settings → Users) or answer on the dashboard. Otherwise you can "
                    "still comment, use the dashboard chat, or ask an admin to override "
                    "from the dashboard.")
                return True
            except (ValueError, KeyError) as e:
                await self.clickup.comment(parent_task,
                                           f"{GATE_PREFIX} could not apply `{text[:80]}`: {e}")
            if use_marker:
                self.store.set_fields(job["issue_id"], comment_marker=c["id"])
            return True
        return False

    # ---------- ClickUp as an intake channel ----------

    async def _poll_intake(self):
        """Adopt human-created tickets in the autofix list: '[fix] …' /
        '[bug] …' / '[task] …' queue the 2-phase request flow, '[feature] …'
        the P0-P9 pipeline, '[sentry <issue id>] …' a forced sentry run — the
        ClickUp mirror of the dashboard's intake forms. Engine-created tickets
        never match (their names start '[<project>] …') and every adopted or
        rejected ticket gets a job row, so the scan is idempotent."""
        if not self.settings.clickup_intake_enabled:
            return
        tasks = await self.clickup.list_tasks()
        for t in tasks or []:
            m = INTAKE_RE.match(t.get("name") or "")
            if not m:
                continue
            if self.store.job_for_clickup_task(t["id"]):
                continue  # already adopted (or engine-created)
            kind = m.group(1).lower()
            arg = (m.group(2) or "").strip()
            title = m.group(3).strip()
            full = await self.clickup.get_task(t["id"])  # list payloads omit bodies
            desc = (full or {}).get("description") or ""
            try:
                await self._intake_from_clickup(t, kind, arg, title, desc)
            except Exception:
                log.exception("clickup intake failed for ticket %s", t["id"])

    async def _intake_from_clickup(self, t: dict, kind: str, arg: str,
                                   title: str, desc: str):
        task_id, task_url = t["id"], t.get("url") or ""

        def reject(why: str) -> str:
            # a skipped row pins the ticket so the scan never re-processes it
            # (and the inbox shows WHY the adoption failed)
            job_id = f"cu-{task_id}"
            self.store.insert(job_id, source="clickup", forced=True,
                              title=title, project="", kind="task")
            self.store.set_fields(job_id, clickup_task_id=task_id,
                                  clickup_task_url=task_url)
            self.store.set_status(job_id, "skipped", detail=why)
            return why

        if kind == "sentry":
            if not self.settings.sentry_enabled:
                # reject-pin, never a silent skip: an unresolvable short id on a
                # Sentry-less instance would otherwise rescan (and re-fail) forever
                why = reject("Sentry integration is not configured on this instance "
                             "(SENTRY_ORG / SENTRY_AUTH_TOKEN) — cannot adopt a "
                             "[sentry] ticket")
                await self.clickup.comment(task_id, f"{GATE_PREFIX} could not adopt: {why}")
                return
            issue_id = arg if arg.isdigit() else None
            if issue_id is None and arg:
                resolved = await self.sentry.resolve_short_id(arg.upper())
                if resolved is None:
                    return  # transient Sentry failure — retry next scan, no pin
                issue_id = resolved or None
            if not issue_id:
                why = reject("a sentry adoption needs the issue id or short code: "
                             f"'[sentry 123456] title' or '[sentry WEB-3Y] title'"
                             + (f" — '{arg}' did not resolve" if arg else ""))
                await self.clickup.comment(task_id, f"{GATE_PREFIX} could not adopt: {why}")
                return
            decision = self.intake(issue_id, source="clickup", forced=True, title=title)
            if "queued" in decision:
                # attach BEFORE any await so the run adopts this ticket instead
                # of creating its own
                self.store.set_fields(issue_id, clickup_task_id=task_id,
                                      clickup_task_url=task_url)
            else:
                reject(decision)  # pin the ticket, or the scan re-comments forever
            await self.clickup.comment(task_id, f"{GATE_PREFIX} 📥 {decision}")
            log.info("clickup intake: sentry %s from ticket %s (%s)", issue_id, task_id, decision)
            return

        if kind == "memory":
            project = arg.strip()
            if self.settings.repo_for_project(project) is None:
                why = reject(f"no repo mapped for project '{project or '(missing)'}' — "
                             "use '[memory <project slug>] title'")
                await self.clickup.comment(task_id, f"{GATE_PREFIX} could not adopt: {why}")
                return
            decision = self.intake_memory(project)
            if "queued" in decision:
                self.store.set_fields(f"mem-{project}", clickup_task_id=task_id,
                                      clickup_task_url=task_url)
            else:
                reject(decision)  # pin the ticket, or the scan re-comments forever
            await self.clickup.comment(task_id, f"{GATE_PREFIX} 📥 {decision}")
            log.info("clickup intake: memory %s from ticket %s (%s)", project, task_id, decision)
            return

        pm = PROJECT_LINE_RE.search(desc)
        project = (pm.group(1) if pm else "").strip()
        if self.settings.repo_for_project(project) is None:
            why = reject(f"no repo mapped for project '{project or '(missing)'}' — put a "
                         "'project: <slug>' line in the task description")
            await self.clickup.comment(task_id, f"{GATE_PREFIX} could not adopt: {why}")
            return
        request = PROJECT_LINE_RE.sub("", desc).strip() or title

        if kind == "feature":
            # the workflow contract: the ticket creator sets the DRI people
            # fields (names configured via clickup_dri_field_map — read-side
            # lookups against the customer's own schema, quiet no-op when the
            # fields don't exist); BOTH roles are captured independently
            # (Epic A2) and the legacy `owner` alias is computed at intake
            fields = await self.clickup.task_fields(task_id)
            try:
                dri_map = json.loads(self.settings.clickup_dri_field_map) or {}
            except (ValueError, TypeError):
                dri_map = {}

            def _pid(v):
                return str((v[0] or {}).get("id") or "") if isinstance(v, list) and v else ""

            founder_dri = _pid(fields.get(str(dri_map.get("founder") or "").lower()))
            dev_dri = _pid(fields.get(str(dri_map.get("dev") or "").lower()))
            # Epic B1: metric goal lines in the description ('metric:'/'target:'/
            # 'window:'), stripped from the request exactly like the project
            # line; the ticket's `Success metric` custom field is the fallback
            # when no metric: line exists
            mm = METRIC_LINE_RE.search(desc)
            tm = TARGET_LINE_RE.search(desc)
            wm = WINDOW_LINE_RE.search(desc)
            success_metric = _clean_metric_value(mm.group(1)) if mm else ""
            metric_target = _clean_metric_value(tm.group(1)) if tm else ""
            window_days = None
            if wm:
                days = int(wm.group(1))
                if 1 <= days <= 365:  # same clamp as the API — never silently accept
                    window_days = days
            if not success_metric:
                cu_metric = fields.get("success metric")
                if isinstance(cu_metric, str) and cu_metric.strip():
                    # unlike the METRIC_LINE_RE path (single-line by
                    # construction), a custom-field value can span lines —
                    # collapse it, same bound as everywhere else
                    success_metric = _single_line(cu_metric)
            for rx in (METRIC_LINE_RE, TARGET_LINE_RE, WINDOW_LINE_RE):
                request = rx.sub("", request)
            request = request.strip() or title
            job_id = f"feat-{task_id}"
            decision = self.intake_feature(
                job_id, title=title, project=project, request=request,
                clickup_task_id=task_id, clickup_task_url=task_url,
                founder_dri=founder_dri, dev_dri=dev_dri,
                success_metric=success_metric, metric_target=metric_target,
                metric_window_days=window_days,
                cu_list_id=t.get("list_id") or self.settings.clickup_list_id)
            adopted_as = ("FEATURE PIPELINE (P0 Intake → P9 Ship) — each stage posts its "
                          "artifact as a subtask and parks here for your `/proceed`, "
                          "`/redo` or `/skip`")
        else:  # fix / bug / task — the 2-phase request flow
            job_id = f"task-{task_id}"
            decision = self.intake_task(
                job_id, title=title, project=project, request=request,
                clickup_task_id=task_id, clickup_task_url=task_url)
            adopted_as = ("change request — I analyse the code first and post my plan + "
                          "questions here; reply `/proceed <guidance>` or `/skip`")
        await self.clickup.comment(
            task_id, f"{GATE_PREFIX} 📥 adopted as a {adopted_as}. ({decision})")
        log.info("clickup intake: %s %s from ticket %s (%s)", kind, job_id, task_id, decision)

    async def slack_ingest_forever(self):
        """Epic D3 (FLAG, off by default): poll allowlisted Slack channels for
        decision-shaped messages and park them as registry CANDIDATES — inbox
        items for human confirmation, never auto-committed, no job state ever
        touched. Plain worker loop today (sla_forever shape) so Epic I1's
        routine engine can adopt it as a routine kind without rework."""
        if not (self.settings.slack_ingest_enabled and self.settings.slack_bot_token):
            log.info("slack ingestion disabled (flag off or no bot token)")
            return
        while True:
            await asyncio.sleep(self.settings.slack_ingest_interval_seconds)
            try:
                await self._slack_ingest_once()
            except Exception:
                log.exception("slack ingest pass failed")

    def _slack_reader(self, transport=None):
        from .slack_ingest import SlackReader

        return SlackReader(self.settings, transport=transport)

    async def _slack_ingest_once(self, transport=None):
        """One ingest pass. Per-channel isolation: one bad channel never
        starves the rest. Everything best-effort + logged."""
        if not (self.settings.slack_ingest_enabled and self.settings.slack_bot_token):
            return
        if not self.workspaces:
            return
        reader = self._slack_reader(transport=transport)
        for ws in self.store.workspace_list():
            channels = self.workspaces.slack_channels_of(ws)
            for channel in channels:
                try:
                    await self._slack_ingest_channel(reader, ws, channel)
                except Exception:
                    log.exception("slack ingest failed for channel %s", channel)

    async def _slack_ingest_channel(self, reader, ws: dict, channel: str):
        from . import slack_ingest

        cursor = self.store.slack_cursor_get(channel)
        if cursor is None:
            # never initialized (hand-edited row / pre-init channel): start
            # NOW and ingest forward only — no historical candidate flood
            self.store.slack_cursor_set(channel, f"{time.time():.6f}")
            return
        # bounded overlap re-scan so late reactions on recent messages are
        # seen (conversations.history keys on ORIGINAL ts); the (source, ref)
        # dedupe absorbs the re-reads. Reactions older than the overlap are a
        # documented limit.
        oldest = f"{max(0.0, float(cursor) - slack_ingest.RESCAN_OVERLAP_SECONDS):.6f}"
        messages: list[dict] = []
        page_cursor = ""
        for _ in range(slack_ingest.MAX_PAGES_PER_PASS):
            page = await reader.history(channel, oldest, cursor=page_cursor)
            if page["status"] != "ok":
                # a failed page aborts THIS channel without advancing the
                # watermark — a partial fetch must never skip messages
                log.warning("slack history failed for %s: %s", channel,
                            page.get("detail") or "")
                return
            messages.extend(page["messages"])
            if not (page["has_more"] and page["next_cursor"]):
                break
            page_cursor = page["next_cursor"]
        else:
            # pagination bound hit with more remaining: process what we have
            # but do NOT advance past it — the next pass continues
            log.warning("slack channel %s: pagination bound hit; continuing "
                        "next pass", channel)
        emoji = self.settings.slack_decision_emoji
        max_ts = float(cursor)
        for msg in messages:
            ts = str(msg.get("ts") or "")
            if not ts:
                continue
            if slack_ingest.is_decision_shaped(msg, emoji):
                fields = slack_ingest.candidate_fields(msg)
                if not fields["text"]:
                    continue
                link = await reader.permalink(channel, ts)
                try:
                    did = self.store.decision_add(
                        "slack", fields["text"], ref=f"{channel}:{ts}",
                        status="candidate", scope="product",
                        workspace_id=ws["id"], title=fields["title"],
                        decided_by=fields["decided_by"],
                        origin_author=fields["decided_by"],
                        links=[link] if link else [])
                    if did is not None:
                        log.info("slack candidate #%s from %s (%s)", did,
                                 channel, ts)
                except ValueError:
                    pass  # empty text — guarded above, belt and braces
            try:
                max_ts = max(max_ts, float(ts))
            except ValueError:
                continue
        # advance the watermark ONLY after the whole batch committed (a crash
        # re-fetches; the dedupe absorbs). Dismissed candidates stay dismissed:
        # their (source, ref) row is kept, so a re-scan can never re-create one.
        if max_ts > float(cursor):
            self.store.slack_cursor_set(channel, f"{max_ts:.6f}")

    async def autonomy_forever(self):
        """Nightly autonomy scorer (Epic C1, docs/ENGINE.md §15). The flag is
        checked once — flipping AUTONOMY_ENABLED requires a restart (env-only,
        documented in OPERATIONS.md). compute() is synchronous SQLite work, so
        it runs in a thread — a full 30-day scan must never block gates or
        webhooks on the event loop."""
        if not self.settings.autonomy_enabled:
            log.info("autonomy disabled; scorer not started")
            return
        await asyncio.sleep(120)  # settle after deploy, then first pass immediately
        while True:
            try:
                res = await asyncio.to_thread(
                    autonomy.compute, self.store, self.settings)
                log.info("autonomy recompute: %(cells)d cells, %(changed)d level changes", res)
            except Exception:
                log.exception("autonomy recompute failed")
            await asyncio.sleep(self.settings.autonomy_recompute_hours * 3600)

    # Epic I1: the sweep/reaper/janitor `while True` wrappers moved into the
    # routine scheduler (app/routines.py — builtin rows, settings-derived
    # cadence, boot settle bump). The `_once` bodies below are unchanged. The
    # shepherd, SLA, watch, autonomy and ClickUp-poll loops deliberately stay
    # native worker loops (control-flow-adjacent / tight cadence — and the
    # shepherd invokes Claude, which a routine never may).

    async def _sweep_once(self):
        known = self.store.known_issue_ids()
        candidates = await self.sentry.top_unresolved_issues(limit=25)
        picked = 0
        for issue in candidates:
            issue_id = str(issue.get("id"))
            if issue_id in known:
                continue
            decision = self.intake(issue_id, source="sweep")
            log.info("sweep: %s", decision)
            picked += 1
            if picked >= self.settings.sweep_top_n:
                break
        log.info("sweep done: %d candidates enqueued (grading decides the rest)", picked)

    def _janitor_once(self):
        """Daily janitor body (now driven by the routine scheduler): prune run
        transcripts by mtime (no keep-set — replay history, not resume state)
        and, with session persistence on, prune CLI session transcripts with
        the keep-set. Inbox-notice expiry + routine-run retention ride the
        janitor routine handler (app/routines.py)."""
        pruned = transcripts.prune(self.settings, self.settings.transcript_ttl_days)
        if pruned:
            log.info("transcript janitor pruned %d run transcripts", pruned)
        if self.settings.session_persistence:
            self._prune_sessions()

    def _prune_sessions(self):
        keep: set[str] = set()
        for j in self.store.by_status(["received", "queued", "running", "awaiting_input"]):
            if j.get("resume_session_id"):
                keep.add(j["resume_session_id"])
            for r in self.store.stage_runs_for(j["issue_id"]):
                if r.get("session_id"):
                    keep.add(r["session_id"])
            for t in self.store.chat_for(j["issue_id"]):
                if t.get("session_id"):
                    keep.add(t["session_id"])
        cutoff = time.time() - self.settings.session_ttl_days * 86400
        pruned = 0
        # both session stores: the stage/fork store AND the artifact-primed chat
        # store — chats write their transcripts to the second one
        for config_dir in (self.settings.claude_config_dir,
                           self.settings.claude_chat_config_dir):
            root = Path(config_dir) / "projects"
            if not root.is_dir():
                continue
            for f in root.glob("*/*.jsonl"):
                try:
                    if f.stem not in keep and f.stat().st_mtime < cutoff:
                        f.unlink()
                        pruned += 1
                except OSError:
                    continue
        if pruned:
            log.info("session janitor pruned %d transcripts", pruned)

    # ---------- the PR shepherd (autonomous Sentry-review loop) ----------

    SHEPHERD_VERDICT_RE = re.compile(
        r"^FINDING\s+(\d+):\s*(FIXED|REBUT)\s*[—:-]*\s*(.*)$", re.MULTILINE)
    SHEPHERD_STATES = ("ready", "in_review", "changes_requested")
    # States the review loop never drives, but whose merge/close MUST still be
    # detected (Epic B4): the mainline flow parks a PR at 'approved' ("ready
    # to merge") before the human merges it on GitHub — without re-polling,
    # prs.state never becomes 'merged', the outcome watch never spawns, and
    # the P9-approval fallback reads the same stale state. 'stalled' (round
    # cap) and 'draft' (pr_auto_ready off) PRs get merged by humans too.
    MERGE_SCAN_STATES = ("approved", "stalled", "draft")

    async def shepherd_forever(self):
        """Drive every tracked PR through Sentry review autonomously: verify
        each finding, fix it on the PR branch, reply on the thread, re-trigger
        `@sentry review` (replies alone never re-engage the bot), and repeat
        until the clean-pass 🎉 lands — or the round cap hands off to a human."""
        while True:
            await asyncio.sleep(self.settings.shepherd_interval_seconds)
            try:
                await self._shepherd_pass()
            except Exception:
                log.exception("shepherd pass failed")

    async def _shepherd_pass(self):
        if not (self.settings.shepherd_enabled and self.engine.github.enabled):
            return
        for pr in self.store.prs_in_state(self.SHEPHERD_STATES + self.MERGE_SCAN_STATES):
            try:
                await self._shepherd_pr(pr)
            except Exception:
                log.exception("shepherd failed for %s", pr["url"])
                self.store.pr_set(pr["url"], detail="shepherd error — will retry")
            finally:
                self.store.pr_set(pr["url"], last_checked=time.time())

    async def _shepherd_pr(self, pr: dict):
        gh = self.engine.github
        repo, number, url = pr["repo"], pr["number"], pr["url"]
        if not repo or not number:
            return
        info = await gh.get_pr(repo, number)
        if info is None:
            return  # unknown, never 'closed' — try again next pass
        if info.get("merged") or info.get("merged_at"):
            self.store.pr_set(url, state="merged", detail="")
            await self._shepherd_notify(pr, f"PR {repo}#{number} merged.")
            job = self.store.get(pr["job_id"])
            if job:  # conveyor mirror: merged feature slides to Complete
                await self.engine.sync_stage_field(job, "merged")
                # outcome loop (Epic B4): a merged, TERMINAL feature starts its
                # watch (the guard inside refuses mid-pipeline merges; the
                # P9-approval path covers PRs merged before approval)
                await self._maybe_spawn_watch(job)
            return
        if info.get("state") == "closed":
            self.store.pr_set(url, state="closed", detail="closed without merge")
            return
        if (pr.get("state") or "") in self.MERGE_SCAN_STATES:
            # merge/close detection ONLY for approved/stalled/draft rows: the
            # review loop stays terminal for 'approved' (post-approval pushes
            # re-kick via record_prs), handed off for 'stalled', and off by
            # operator choice for 'draft' — never resume driving from here
            return
        if info.get("draft"):
            # the kickoff's un-draft failed earlier — retry before anything else
            if await gh.mark_ready(repo, number):
                self.store.pr_set(url, state="ready")
            else:
                self.store.pr_set(url, detail="still draft — could not mark ready")
                return

        comments = await gh.list_comments(repo, number)
        if comments is None:
            return
        triggers = [c for c in comments
                    if (c.get("body") or "").strip().startswith("@sentry review")]
        if triggers:
            reactions = await gh.get_comment_reactions(repo, triggers[-1]["id"])
            if reactions is None:
                return  # unknown ≠ no reactions — retry next pass, same as get_pr
            if any(r.get("content") == "hooray" for r in reactions):
                # stamp the approved head: a later run pushing MORE commits to
                # this PR flips it back to in_review (engine.record_prs compares
                # heads) so post-approval work never ships unreviewed
                head = ((info.get("head") or {}).get("sha") or "").strip()
                self.store.pr_set(url, state="approved", detail="Sentry clean pass",
                                  approved_head=head)
                await self._shepherd_notify(
                    pr, f"PR {repo}#{number} approved by Sentry — ready to merge. {url}")
                return

        review_comments = await gh.get_review_comments(repo, number)
        if review_comments is None:
            return
        # a finding is OPEN while the bot has not edited it to '*Resolved in …*';
        # it is UNREPLIED until one of our replies hangs off it (rebuts stay
        # replied so they never re-fix — the re-trigger lets the bot re-judge)
        replied_to = {c.get("in_reply_to_id") for c in review_comments if c.get("in_reply_to_id")}
        open_findings = [c for c in review_comments
                         if not c.get("in_reply_to_id")
                         and "BUG_PREDICTION" in (c.get("body") or "")
                         and "Resolved in" not in (c.get("body") or "")[:200]]
        if not open_findings:
            if not triggers:
                # the kickoff marked this PR ready but its trigger comment never
                # landed (transient failure) — no review was EVER requested, so
                # "waiting" here would deadlock. Recover by requesting one now.
                if await gh.comment(repo, number, "@sentry review"):
                    rounds = int(pr.get("review_rounds") or 0) + 1
                    self.store.pr_set(url, state="in_review", review_rounds=rounds,
                                      detail=f"round {rounds}: recovered the missing "
                                             "review trigger")
                return
            return  # a pass is in flight — wait for findings or the 🎉
        if int(pr.get("review_rounds") or 0) >= self.settings.pr_max_review_rounds:
            self.store.pr_set(url, state="stalled",
                              detail=f"max review rounds ({self.settings.pr_max_review_rounds}) "
                                     "reached — needs a human")
            await self._shepherd_notify(
                pr, f"PR {repo}#{number}: Sentry review did not converge after "
                    f"{self.settings.pr_max_review_rounds} rounds — please take over. {url}")
            return

        unreplied = [c for c in open_findings if c["id"] not in replied_to]
        if not unreplied:
            # every open finding already carries our reply (fix or rebut) and got
            # its trigger in THAT pass — re-triggering every pass here would burn
            # the round cap while the bot simply hasn't re-judged yet. Wait.
            self.store.pr_set(url, detail="all findings replied — awaiting re-review")
            return

        branch = ((info.get("head") or {}).get("ref") or "").strip()
        verdicts = await self._shepherd_fix(pr, branch, unreplied)
        if verdicts is None:
            self.store.pr_set(url, detail="fix run failed — will retry")
            return
        handled = 0
        for c in unreplied:
            v = verdicts.get(c["id"])
            if not v:
                # NEVER claim FIXED for a finding the run did not report — a false
                # reply marks it handled forever and the same bug just gets
                # re-flagged every round. Left unreplied, the NEXT pass re-attempts.
                log.warning("shepherd: no verdict for finding %s on %s", c["id"], url)
                continue
            kind, summary = v
            prefix = "Fixed — " if kind == "FIXED" else "Not a real issue — "
            await gh.reply_to_review_comment(repo, number, c["id"],
                                             prefix + summary[:800])
            handled += 1
        if handled == 0:
            self.store.pr_set(url, detail="run returned no verdicts — will retry")
            return
        # one explicit trigger per round of actual work — pushes/replies alone
        # never re-engage the bot (learned shepherding this repo's own PRs)
        if await gh.comment(repo, number, "@sentry review"):
            rounds = int(pr.get("review_rounds") or 0) + 1
            self.store.pr_set(url, state="in_review", review_rounds=rounds,
                              detail=f"round {rounds}: {handled} finding(s) addressed")

    async def _shepherd_fix(self, pr: dict, branch: str, findings: list[dict]) -> dict | None:
        """One headless verify-and-fix run on the PR branch. Returns
        {finding_id: (FIXED|REBUT, summary)} parsed from the output protocol,
        or None when the run could not complete."""
        target = self.settings.target_for_repo(pr["repo"])
        if target is None or not branch:
            self.store.pr_set(pr["url"], detail=f"no repo target for {pr['repo']}")
            return None
        spname, sbrief = self._job_context(self.store.get(pr["job_id"]) or {})
        prompt = build_shepherd_prompt(
            target=target, pr_url=pr["url"], branch=branch,
            findings=[{"id": c["id"], "path": c.get("path"),
                       "line": c.get("line") or c.get("original_line"),
                       "body": c.get("body")} for c in findings],
            product_name=spname, business_context=sbrief)
        # a DEDICATED clone + lock, NOT the main repo lock: a fix run can hold a
        # lock for a full claude timeout, and taking the main one would starve
        # pipeline stages / sentry jobs on that repo for the duration. The run
        # mutates only its own clone and pushes to origin, so this is safe by
        # construction (same pattern as the v1 chat clone); the shepherd lock
        # just serializes shepherd runs per repo.
        async with self.locks.for_repo(f"shepherd:{target.repo}"):
            try:
                workspace = await prepare_feature_workspace(
                    self.settings, target, branch, stage=1,
                    workspace_root=f"{self.settings.workspaces_dir}/shepherd")
            except (BranchLostError, RuntimeError) as e:
                self.store.pr_set(pr["url"], detail=f"cannot check out {branch}: {str(e)[:160]}")
                return None
            raw = await run_claude_raw(
                self.settings, workspace, prompt,
                allowed_tools=BASE_ALLOWED_TOOLS + target.allow,
                timeout=self.settings.claude_timeout_seconds)
        if raw.status != "ok":
            return None
        return {int(m.group(1)): (m.group(2), m.group(3).strip())
                for m in self.SHEPHERD_VERDICT_RE.finditer(raw.text)}

    async def _shepherd_notify(self, pr: dict, message: str):
        """Surface a shepherd milestone on the owning job's ClickUp ticket."""
        job = self.store.get(pr["job_id"])
        if job:
            await self.clickup.comment(job.get("clickup_task_id") or "",
                                       f"{GATE_PREFIX} 🐑 {message}")

    # ---------- gate SLA & escalation (Epic A5) ----------

    async def sla_forever(self):
        """Escalate overdue gates: nudge the owner → notify the other DRI
        (visibility, never authority) → flag for the standup surface. Pure
        visibility — no job state changes, no CAS involvement."""
        while True:
            await asyncio.sleep(self.settings.sla_check_interval_seconds)
            try:
                await self._sla_once()
            except Exception:
                log.exception("sla sweep failed")

    async def _sla_once(self):
        now = time.time()
        for job in self.store.by_status(["awaiting_input"]):
            if (job.get("kind") or "") != "feature":
                continue  # v1 items have no DRIs; the inbox overdue flag covers them
            ws = self._ws_row(job)
            sla = ws["gate_sla_hours"] if ws and ws.get("gate_sla_hours") is not None \
                else self.settings.gate_sla_hours
            if not sla:
                continue  # 0 disables escalation
            owner = roles.gate_owner(self.store, self.settings, ws, job)
            if owner is None or not owner.enforce:
                # inert without explicit DRIs: solo installs (and pre-upgrade
                # legacy-owner jobs) get NO new noise — exactly as today
                continue
            stage = int(job.get("stage") or 0)
            # the ladder keys on the gated RUN's id: globally unique, so a
            # /redo re-park (new run) re-arms it, and a restarted pipeline's
            # fresh gates can never collide with a dead pipeline's rows
            runs = [r for r in self.store.stage_runs_for(job["issue_id"])
                    if r["stage"] == stage and r["gate_posted_at"]]
            if not runs:
                continue
            run = runs[-1]
            waited = now - run["gate_posted_at"]
            if waited < sla * 3600:
                continue
            await self._sla_escalate(job, ws, owner, stage, run, waited, sla)

    async def _sla_escalate(self, job, ws, owner, stage, run, waited, sla):
        """One overdue gate's escalation ladder. Each step records its
        gate_event BEFORE any send (crash = under-notify, never double-fire);
        UNIQUE(job, kind, ref) makes every step fire exactly once per gate."""
        job_id = job["issue_id"]
        task_id = job.get("clickup_task_id") or ""
        h = int(waited // 3600)
        title = job.get("title") or job_id
        # step 1 (≥ 1.0×SLA): re-nudge the owning DRI
        if self.store.gate_event_add(job_id, "sla_nudge", ref=f"run{run['id']}-step1",
                                     stage=stage, actor="engine",
                                     detail=f"waited {h}h of {sla}h SLA"):
            if owner.clickup_id:
                await self.clickup.set_assignee(task_id, owner.clickup_id)
            await self.clickup.comment(
                task_id, f"{GATE_PREFIX} ⏰ this P{stage} gate has waited {h}h "
                         f"(SLA {sla}h) — {owner.display}, it needs your `/proceed`, "
                         "`/redo` or `/skip`.")
            if self.workspaces:
                await self.workspaces.notify_gate(
                    job, f"⏰ {title} — P{stage} gate over SLA ({h}h > {sla}h), "
                         f"waiting on {owner.display}.")
        # step 2 (≥ 1.5×SLA): notify the OTHER DRI — visibility, not authority.
        # When the owning role's DRI slot is empty, gate_owner already fell
        # back to the other role's DRI as the EFFECTIVE owner — the "other"
        # person IS the owner (step 1 nudged them), and telling them they
        # "can't answer" their own gate would be false. Suppress the step.
        other = roles.other_dri(job, owner.role)
        if other == owner.value:
            other = ""
        if other and waited >= 1.5 * sla * 3600:
            if self.store.gate_event_add(job_id, "sla_second_dri",
                                         ref=f"run{run['id']}-step2", stage=stage,
                                         actor="engine",
                                         detail=f"waited {h}h of {sla}h SLA"):
                other_name = roles.dri_display(self.store, other)
                await self.clickup.comment(
                    task_id, f"{GATE_PREFIX} ⏰ {other_name}: this P{stage} "
                             f"{owner.role} gate has waited {h}h (SLA {sla}h) and "
                             f"{owner.display} hasn't answered. You can't answer this "
                             f"{owner.role} gate, but they may need a nudge — or an "
                             "admin can override from the dashboard.")
                if self.workspaces:
                    await self.workspaces.notify_gate(
                        job, f"⏰ {title} — P{stage} gate still unanswered at {h}h; "
                             f"{other_name}, {owner.display} may need a nudge "
                             "(visibility only).")
        # step 3 (≥ 2.0×SLA): record for the standup surface — no more sends
        if waited >= 2.0 * sla * 3600:
            self.store.gate_event_add(job_id, "sla_standup_flag",
                                      ref=f"run{run['id']}-step3", stage=stage,
                                      actor="engine",
                                      detail=f"escalation exhausted at {h}h "
                                             f"of {sla}h SLA")

    def reap_horizon(self) -> float:
        """A 'running' row older than any plausible live run means the process
        died mid-run. Memory bootstraps hold 'running' across TWO full-length
        runs — size for the worst."""
        return 2 * self.settings.claude_timeout_seconds + self.settings.reaper_grace_seconds

    async def _reap_once(self, horizon: float):
        for job in self.store.stale_running(horizon):
            log.warning("reaping stale run: %s (started %.0fs ago)",
                        job["issue_id"], time.time() - (job["run_started_at"] or 0))
            self.store.set_status(job["issue_id"], "error",
                                  detail="reaped: run went stale (process restart?) — redo to resume")
            # visibility on the owning ticket — a silently-reaped job looks
            # identical to a slow one from ClickUp (dogfood-found)
            await self.clickup.comment(
                job.get("clickup_task_id") or "",
                f"{GATE_PREFIX} ⚠️ the run went stale and was reaped — re-kick with "
                "`/redo` (features) or re-file the intake ticket to retry.")

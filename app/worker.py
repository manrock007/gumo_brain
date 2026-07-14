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
import logging
import re
import time
from pathlib import Path

from .clickup import ClickUp
from .config import Settings
from .db import JobStore
from .engine import GATE_PREFIX, Engine, RepoLocks
from .fixer import prepare_workspace, run_claude
from .grading import grade_issue
from .prompts import (
    build_fix_prompt,
    build_phase2_prompt,
    build_task_implement_prompt,
    build_task_plan_prompt,
)
from .sentry_api import SentryClient, format_stacktrace

log = logging.getLogger("brain.worker")

ACTIVE_STATUSES = ("received", "queued", "running")
TERMINAL_STATUSES = ("pr_opened", "no_fix", "skipped", "error", "timeout")

# priority classes: live sentry >= answered feature stages / tasks > sweep
PRIO_SENTRY = 0
PRIO_HUMAN = 1
PRIO_SWEEP = 2

QUESTION_HEADING_RE = re.compile(r"^#{1,4}\s*(?:open\s+)?questions?\b.*$", re.IGNORECASE | re.MULTILINE)
REDO_TARGET_RE = re.compile(r"^\s*[Pp](\d)\b\s*")


class GateConflict(Exception):
    """The gate was already answered through the other channel."""


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
        self._enqueue(issue_id, PRIO_SWEEP if source == "sweep" else PRIO_SENTRY)
        return f"issue {issue_id} queued"

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
        self._enqueue(job_id, PRIO_HUMAN)
        return f"request {job_id} queued"

    def intake_feature(self, job_id: str, title: str, project: str, request: str,
                       clickup_task_id: str | None = None,
                       clickup_task_url: str | None = None,
                       cu_list_id: str = "", owner: str = "",
                       related_jobs: str = "", gate_mode: str = "") -> str:
        """Enqueue a feature pipeline at P0."""
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
            related_jobs=related_jobs,
        )
        self._enqueue(job_id, PRIO_HUMAN)
        return f"feature {job_id} queued at P0"

    def intake_memory(self, project: str) -> str:
        job_id = f"mem-{project}"
        existing = self.store.get(job_id)
        if existing and existing["status"] in ACTIVE_STATUSES:
            return f"memory bootstrap for {project} already in progress ({existing['status']})"
        self.store.insert(job_id, source="manual", forced=True,
                          title=f"memory bootstrap: {project}", project=project, kind="memory")
        self._enqueue(job_id, PRIO_HUMAN)
        return f"memory bootstrap for {project} queued"

    # ---------- main loop ----------

    async def run_forever(self):
        await self.clickup.load_statuses()
        # SQLite is the queue of record: re-enqueue whatever a restart dropped
        for job in self.store.requeueable():
            self._enqueue(job["issue_id"], self._priority_for(job))
            log.info("startup requeue: %s (%s)", job["issue_id"], job["status"])
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

    async def _process(self, job: dict, queued_at: float | None = None):
        """Every workspace toucher runs under its repo's lock (chat runs and the
        canonical product-scope reads take the same locks — see Engine.RepoLocks)."""
        kind = job.get("kind") or "sentry"
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
            created = await self.clickup.create_task(
                name=f"[{project_slug}] {issue.get('title', 'unknown')}",
                description=self._ticket_description(issue, row),
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
        branch = f"brain/sentry-{issue_id}"

        async with self.locks.for_repo(target.repo):  # workspace toucher
            if phase == 2:
                prompt = build_phase2_prompt(
                    target=target, branch=branch, issue=issue_info, stacktrace=stacktrace,
                    clickup_task_id=task_id,
                    analysis=row.get("analysis") or "(analysis missing)",
                    guidance=row.get("guidance") or "(no guidance recorded)",
                )
                workspace = await prepare_workspace(self.settings, target, branch, keep_branch=True)
            else:
                prompt = build_fix_prompt(
                    target=target, branch=branch, issue=issue_info, stacktrace=stacktrace,
                    clickup_task_id=task_id,
                )
                workspace = await prepare_workspace(self.settings, target, branch)

            log.info("running claude for issue %s phase %s (%s)", issue_id, phase, target.repo)
            result = await run_claude(self.settings, target, workspace, prompt)
        log.info("issue %s -> %s %s", issue_id, result.status, result.pr_url or "")

        if result.status == "needs_input":
            await self._park_awaiting(issue_id, task_id or "", result.detail)
            return

        self.store.set_status(issue_id, result.status, pr_url=result.pr_url, detail=result.detail)
        await self.clickup.set_status(task_id or "", result.status)

        if result.status == "pr_opened":
            await self.clickup.comment(task_id or "", f"Draft PR opened: {result.pr_url}")
            await self.sentry.post_comment(
                issue_id,
                f"gumo_brain opened a draft PR for this issue: {result.pr_url}"
                + (f" (tracking: {row.get('clickup_task_url')})" if row.get("clickup_task_url") else ""),
            )
        elif result.status == "no_fix":
            analysis = result.detail.split("NO_FIX:", 1)[-1].strip()[:1500]
            await self.clickup.comment(task_id or "", f"No PR opened.\n\n{analysis}")
            await self.sentry.post_comment(
                issue_id, f"gumo_brain investigated but did not open a PR:\n\n{analysis}"
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
        branch = f"brain/{job_id}"

        if phase == 2:
            prompt = build_task_implement_prompt(
                target=target, branch=branch, task=task_info, request=request_text,
                clickup_task_id=task_id or None,
                analysis=row.get("analysis") or "(analysis missing)",
                guidance=row.get("guidance") or "(no guidance recorded)",
            )
            workspace = await prepare_workspace(self.settings, target, branch, keep_branch=True)
        else:
            prompt = build_task_plan_prompt(
                target=target, branch=branch, task=task_info, request=request_text,
                clickup_task_id=task_id or None,
            )
            workspace = await prepare_workspace(self.settings, target, branch)

        log.info("running claude for request %s phase %s (%s)", job_id, phase, target.repo)
        result = await run_claude(self.settings, target, workspace, prompt)
        log.info("request %s -> %s %s", job_id, result.status, result.pr_url or "")

        if result.status == "needs_input":
            await self._park_awaiting(job_id, task_id, result.detail)
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

    # ---------- HITL: parking and answering ----------

    async def _park_awaiting(self, job_id: str, task_id: str, analysis: str):
        """Park a task/sentry job. Crash-safe ordering: DB transition (with the
        pre-comment marker) commits BEFORE the ClickUp comment is posted."""
        comments = await self.clickup.comments(task_id)
        marker = comments[-1]["id"] if comments else ""
        self.store.set_fields(job_id, analysis=analysis,
                              question=extract_questions(analysis), comment_marker=marker)
        self.store.set_status(job_id, "awaiting_input", detail=analysis[:2000])
        await self.clickup.comment(
            task_id,
            "**Human input needed before I change anything.**\n\n"
            f"{analysis}\n\n---\n"
            "Reply here with `/proceed <your decision/guidance>` to continue, or `/skip` "
            "to drop this — or answer directly on the gumo_brain dashboard.",
        )
        await self.clickup.set_status(task_id, "awaiting_input")

    async def answer_job(self, job_id: str, action: str, text: str, via: str) -> str:
        """Single resolution path for gate answers from BOTH channels.
        Returns the new status. Raises KeyError (unknown), ValueError (invalid
        action/state), GateConflict (lost the CAS race)."""
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        kind = job.get("kind") or "sentry"
        if kind == "feature":
            return await self._answer_feature(job, action, text, via)
        if action == "redo":
            raise ValueError(f"redo is only valid for feature pipelines, not kind '{kind}'")
        return await self._answer_v1(job, action, text, via)

    async def _answer_v1(self, job: dict, action: str, text: str, via: str) -> str:
        job_id = job["issue_id"]
        if job["status"] != "awaiting_input":
            raise ValueError(f"job is '{job['status']}', not awaiting_input")
        task_id = job.get("clickup_task_id") or ""

        if action == "skip":
            if not self.store.cas_status(job_id, ["awaiting_input"], "skipped",
                                         question="", detail=f"skipped by human via {via}"):
                raise GateConflict("already answered")
            if via == "dashboard":
                await self.clickup.comment(task_id, f"{GATE_PREFIX} Decision (via dashboard): skip"
                                                    + (f" — {text}" if text else ""))
            await self.clickup.set_status(task_id, "skipped")
            return "skipped"

        guidance = text or "Proceed as you proposed."
        if not self.store.cas_status(job_id, ["awaiting_input"], "queued",
                                     guidance=guidance, phase=2, question=""):
            raise GateConflict("already answered")
        if via == "dashboard":
            await self.clickup.comment(task_id, f"{GATE_PREFIX} Decision (via dashboard): proceed — {guidance}")
        else:
            await self.clickup.comment(task_id, "Got it — proceeding with the fix now.")
        self._enqueue(job_id, PRIO_HUMAN)
        log.info("job %s advanced to phase 2 via %s", job_id, via)
        return "queued"

    async def _answer_feature(self, job: dict, action: str, text: str, via: str) -> str:
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
                                         resume_head="", resume_answer="", ask_count=0):
                raise GateConflict("already answered")
            self.store.guidance_add(job_id, target_stage, "redo", text, via,
                                    job.get("parked_head") or "")
            self.store.stage_run_gate_answered(job_id, stage, "redo")
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
            self.store.guidance_add(job_id, stage, "answer", answer, via,
                                    job.get("parked_head") or "")
            self.store.stage_run_gate_answered(job_id, stage, "answer")
            self._distill_chat(job, stage)
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
            self.store.guidance_add(job_id, stage, "proceed", guidance, via,
                                    job.get("parked_head") or "")
            self.store.stage_run_gate_answered(job_id, stage, "proceed")
            await self.clickup.comment(
                task_id, f"{GATE_PREFIX} P9 approved (via {via}) — pipeline complete. "
                         f"{'PR ready to un-draft: ' + job['pr_url'] if job.get('pr_url') else ''}")
            await self.clickup.set_status(task_id, final)
            return final

        if not self.store.cas_status(job_id, ["awaiting_input"], "queued",
                                     expected_stage=stage,
                                     stage=stage + 1, stage_attempts=0, question="",
                                     ask_count=0):
            raise GateConflict("already answered")
        self.store.guidance_add(job_id, stage, "proceed", guidance, via,
                                job.get("parked_head") or "")
        self.store.stage_run_gate_answered(job_id, stage, "proceed")
        self._distill_chat(job, stage)
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
            "_Automated fix attempt by gumo_brain. Claude posts progress below. "
            "If it asks for input, reply `/proceed <guidance>` or `/skip`._"
        )

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
        runs = self.store.stage_runs_for(job["issue_id"])
        stamps = [r["gate_posted_at"] for r in runs
                  if r["stage"] == job.get("stage") and r["gate_posted_at"]]
        return max(stamps) if stamps else job.get("updated_at") or 0

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
                if text.startswith(GATE_PREFIX):
                    if use_marker:
                        self.store.set_fields(job["issue_id"], comment_marker=c["id"])
                    continue
                # a human replied conversationally — never drop it silently
                # (docs/CONVERSATIONS.md §2): nudge once per comment, keep scanning
                if (use_marker and job["status"] == "awaiting_input"
                        and (job.get("kind") or "") == "feature" and text):
                    await self.clickup.comment(
                        job.get("clickup_task_id") or "",
                        f"{GATE_PREFIX} I only act on `/proceed`, `/redo` or `/skip` here — "
                        f"did you mean `/proceed {text[:120]}`? "
                        "(For back-and-forth questions, use the chat box on the dashboard.)",
                    )
                    self.store.set_fields(job["issue_id"], comment_marker=c["id"])
                continue
            try:
                await self.answer_job(job["issue_id"], action, payload, via="clickup")
            except GateConflict:
                pass  # answered elsewhere — fine
            except (ValueError, KeyError) as e:
                await self.clickup.comment(job.get("clickup_task_id") or "",
                                           f"{GATE_PREFIX} could not apply `{text[:80]}`: {e}")
            if use_marker:
                self.store.set_fields(job["issue_id"], comment_marker=c["id"])
            return True
        return False

    async def sweep_forever(self):
        if not self.settings.sweep_enabled:
            return
        await asyncio.sleep(300)  # let the stack settle after deploy
        while True:
            try:
                await self._sweep_once()
            except Exception:
                log.exception("sweep iteration failed")
            await asyncio.sleep(self.settings.sweep_interval_hours * 3600)

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

    async def prune_sessions_forever(self):
        """Daily janitor for session transcripts (docs/CONVERSATIONS.md §4):
        prune by file mtime with a keep-set — never by job terminal status alone,
        which misses abandoned gates and unattributed v1 traffic."""
        if not self.settings.session_persistence:
            return
        while True:
            await asyncio.sleep(86400)
            try:
                self._prune_sessions()
            except Exception:
                log.exception("session janitor failed")

    def _prune_sessions(self):
        root = Path(self.settings.claude_config_dir) / "projects"
        if not root.is_dir():
            return
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
        for f in root.glob("*/*.jsonl"):
            try:
                if f.stem not in keep and f.stat().st_mtime < cutoff:
                    f.unlink()
                    pruned += 1
            except OSError:
                continue
        if pruned:
            log.info("session janitor pruned %d transcripts", pruned)

    async def reap_forever(self):
        """A 'running' row older than any plausible live run means the process
        died mid-run (the subprocess dies with us) — surface it instead of
        letting the job hang forever."""
        # memory bootstraps hold 'running' across TWO full-length runs; size for the worst
        horizon = 2 * self.settings.claude_timeout_seconds + self.settings.reaper_grace_seconds
        while True:
            try:
                for job in self.store.stale_running(horizon):
                    log.warning("reaping stale run: %s (started %.0fs ago)",
                                job["issue_id"], time.time() - (job["run_started_at"] or 0))
                    self.store.set_status(job["issue_id"], "error",
                                          detail="reaped: run went stale (process restart?) — redo to resume")
            except Exception:
                log.exception("reaper iteration failed")
            await asyncio.sleep(300)

"""Serial job worker: grade -> ClickUp ticket -> headless Claude run -> draft PR.

Handles two job kinds:
- sentry: production errors (grading -> fix; HITL only when Claude judges it COMPLEX)
- task:   manually reported requests (HITL always: analysis -> awaiting_input -> implement)

Also runs two background loops:
- ClickUp poller: advances `awaiting_input` jobs when a human replies /proceed or /skip
- Sweep: periodically grades the top unresolved Sentry issues (legacy backlog pickup)
"""

import asyncio
import logging
import re
import time

from .clickup import ClickUp
from .config import Settings
from .db import JobStore
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

QUESTION_HEADING_RE = re.compile(r"^#{1,4}\s*(?:open\s+)?questions?\b.*$", re.IGNORECASE | re.MULTILINE)


def extract_questions(analysis: str) -> str:
    """Pull the `## Questions` section out of a NEEDS_INPUT analysis for the dashboard."""
    m = QUESTION_HEADING_RE.search(analysis or "")
    if m:
        rest = analysis[m.end():]
        nxt = re.search(r"^#{1,4}\s", rest, re.MULTILINE)
        section = (rest[: nxt.start()] if nxt else rest).strip()
        if section:
            return section[:1500]
    return (analysis or "").strip()[-600:]


class Worker:
    def __init__(self, settings: Settings, store: JobStore):
        self.settings = settings
        self.store = store
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.sentry = SentryClient(settings)
        self.clickup = ClickUp(settings)

    # ---------- intake ----------

    def intake(self, issue_id: str, source: str, forced: bool = False,
               title: str = "", project: str = "") -> str:
        """Guardrails + enqueue; returns a human-readable decision."""
        existing = self.store.get(issue_id)
        if existing:
            if existing["status"] in ACTIVE_STATUSES:
                return f"issue {issue_id} already {existing['status']}"
            if existing["status"] == "awaiting_input":
                return f"issue {issue_id} is awaiting human input on {existing['clickup_task_url'] or 'its ticket'}"
            if existing["status"] == "pr_opened":
                return f"issue {issue_id} already has a PR: {existing['pr_url']}"
            cooldown = self.settings.issue_cooldown_hours * 3600
            if not forced and time.time() - existing["updated_at"] < cooldown:
                return f"issue {issue_id} in cooldown ({existing['status']})"

        self.store.insert(issue_id, source=source, forced=forced, title=title, project=project)
        self.queue.put_nowait(issue_id)
        return f"issue {issue_id} queued"

    def intake_task(self, job_id: str, title: str, project: str, request: str,
                    clickup_task_id: str | None = None,
                    clickup_task_url: str | None = None) -> str:
        """Enqueue a manually reported request (bug fix / change request)."""
        existing = self.store.get(job_id)
        if existing:
            if existing["status"] in ACTIVE_STATUSES:
                return f"request {job_id} already {existing['status']}"
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
        self.queue.put_nowait(job_id)
        return f"request {job_id} queued"

    # ---------- main loop ----------

    async def run_forever(self):
        await self.clickup.load_statuses()
        log.info("worker started")
        while True:
            issue_id = await self.queue.get()
            try:
                await self._process(issue_id)
            except Exception as e:
                log.exception("job %s failed", issue_id)
                self.store.set_status(issue_id, "error", detail=str(e)[:2000])
                row = self.store.get(issue_id) or {}
                await self.clickup.comment(
                    row.get("clickup_task_id") or "",
                    f"gumo_brain hit an internal error on this issue: {str(e)[:500]}",
                )
            finally:
                self.queue.task_done()

    async def _process(self, job_id: str):
        row = self.store.get(job_id)
        if row is None:
            return
        if (row.get("kind") or "sentry") == "task":
            await self._process_task(row)
        else:
            await self._process_sentry(row)

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
        """Post the analysis to ClickUp (the record) and park the job for a human."""
        self.store.set_fields(job_id, analysis=analysis, question=extract_questions(analysis))
        await self.clickup.comment(
            task_id,
            "**Human input needed before I change anything.**\n\n"
            f"{analysis}\n\n---\n"
            "Reply here with `/proceed <your decision/guidance>` to continue, or `/skip` "
            "to drop this — or answer directly on the gumo_brain dashboard.",
        )
        # record the current tail of the comment stream; only later comments count
        comments = await self.clickup.comments(task_id)
        marker = comments[-1]["id"] if comments else ""
        self.store.set_fields(job_id, comment_marker=marker)
        self.store.set_status(job_id, "awaiting_input", detail=analysis[:2000])
        await self.clickup.set_status(task_id, "awaiting_input")

    async def resolve_awaiting(self, job_id: str, action: str, answer: str) -> str:
        """Dashboard answer to an awaiting_input job. The decision is posted to the
        ClickUp ticket first so the ticket stays the keeper of record. Returns the
        new job status. Raises KeyError (unknown job) / ValueError (not awaiting)."""
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] != "awaiting_input":
            raise ValueError(f"job is '{job['status']}', not awaiting_input")
        task_id = job.get("clickup_task_id") or ""

        if action == "skip":
            note = f" — {answer}" if answer else ""
            await self.clickup.comment(task_id, f"**Decision (via dashboard):** skip{note}")
            marker = await self._latest_comment_id(task_id, job)
            self.store.set_fields(job_id, comment_marker=marker, question="")
            self.store.set_status(job_id, "skipped", detail="skipped by human via dashboard")
            await self.clickup.set_status(task_id, "skipped")
            return "skipped"

        guidance = answer or "Proceed as you proposed."
        await self.clickup.comment(task_id, f"**Decision (via dashboard):** proceed — {guidance}")
        marker = await self._latest_comment_id(task_id, job)
        self.store.set_fields(job_id, guidance=guidance, phase=2,
                              comment_marker=marker, question="")
        self.store.set_status(job_id, "queued")
        self.queue.put_nowait(job_id)
        log.info("job %s advanced to phase 2 via dashboard", job_id)
        return "queued"

    async def _latest_comment_id(self, task_id: str, job: dict) -> str:
        comments = await self.clickup.comments(task_id)
        return comments[-1]["id"] if comments else (job.get("comment_marker") or "")

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
        for job in self.store.by_status(["awaiting_input"]):
            task_id = job.get("clickup_task_id")
            if not task_id:
                continue
            comments = await self.clickup.comments(task_id)
            marker = job.get("comment_marker") or ""
            seen_marker = not marker
            for c in comments:
                if not seen_marker:
                    seen_marker = c["id"] == marker
                    continue
                text = (c.get("text") or "").strip()
                lowered = text.lower()
                if lowered.startswith("/proceed"):
                    guidance = text[len("/proceed"):].strip() or "Proceed as you proposed."
                    self.store.set_fields(
                        job["issue_id"], guidance=guidance, phase=2,
                        comment_marker=c["id"], question="",
                    )
                    self.store.set_status(job["issue_id"], "queued")
                    self.queue.put_nowait(job["issue_id"])
                    await self.clickup.comment(task_id, "Got it — proceeding with the fix now.")
                    log.info("issue %s advanced to phase 2 via ClickUp", job["issue_id"])
                    break
                if lowered.startswith("/skip"):
                    self.store.set_fields(job["issue_id"], comment_marker=c["id"], question="")
                    self.store.set_status(job["issue_id"], "skipped", detail="skipped by human via ClickUp")
                    await self.clickup.set_status(task_id, "skipped")
                    await self.clickup.comment(task_id, "Understood — dropping this issue.")
                    break

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

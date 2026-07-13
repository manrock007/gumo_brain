"""Serial job worker: grade -> ClickUp ticket -> headless Claude run -> draft PR.

Also runs two background loops:
- ClickUp poller: advances `awaiting_input` jobs when a human replies /proceed or /skip
- Sweep: periodically grades the top unresolved Sentry issues (legacy backlog pickup)
"""

import asyncio
import logging
import time

from .clickup import ClickUp
from .config import Settings
from .db import JobStore
from .fixer import prepare_workspace, run_claude
from .grading import grade_issue
from .prompts import build_fix_prompt, build_phase2_prompt
from .sentry_api import SentryClient, format_stacktrace

log = logging.getLogger("brain.worker")

ACTIVE_STATUSES = ("received", "queued", "running")


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

    async def _process(self, issue_id: str):
        row = self.store.get(issue_id)
        if row is None:
            return
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
            self.store.set_fields(issue_id, analysis=result.detail)
            await self.clickup.comment(
                task_id or "",
                "**Human input needed before I fix this.**\n\n"
                f"{result.detail}\n\n---\n"
                "Reply here with `/proceed <your decision/guidance>` to apply the fix, "
                "or `/skip` to drop this issue.",
            )
            # record the current tail of the comment stream; only later comments count
            comments = await self.clickup.comments(task_id or "")
            marker = comments[-1]["id"] if comments else ""
            self.store.set_fields(issue_id, comment_marker=marker)
            self.store.set_status(issue_id, "awaiting_input", detail=result.detail[:2000])
            await self.clickup.set_status(task_id or "", "awaiting_input")
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
                        job["issue_id"], guidance=guidance, phase=2, comment_marker=c["id"]
                    )
                    self.store.set_status(job["issue_id"], "queued")
                    self.queue.put_nowait(job["issue_id"])
                    await self.clickup.comment(task_id, "Got it — proceeding with the fix now.")
                    log.info("issue %s advanced to phase 2 via ClickUp", job["issue_id"])
                    break
                if lowered.startswith("/skip"):
                    self.store.set_fields(job["issue_id"], comment_marker=c["id"])
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

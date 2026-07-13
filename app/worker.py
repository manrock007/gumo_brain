"""Serial job worker: one Sentry issue -> one headless Claude run -> one PR."""

import asyncio
import logging
import time

from .config import Settings
from .db import JobStore
from .fixer import prepare_workspace, run_claude
from .prompts import build_fix_prompt
from .sentry_api import SentryClient, format_stacktrace

log = logging.getLogger("brain.worker")


class Worker:
    def __init__(self, settings: Settings, store: JobStore):
        self.settings = settings
        self.store = store
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.sentry = SentryClient(settings)

    def try_enqueue(self, issue_id: str, project: str, title: str) -> str:
        """Apply guardrails; returns a human-readable decision."""
        existing = self.store.get(issue_id)
        if existing:
            if existing["status"] in ("queued", "running"):
                return f"issue {issue_id} already {existing['status']}"
            if existing["status"] == "pr_opened":
                return f"issue {issue_id} already has a PR: {existing['pr_url']}"
            cooldown = self.settings.issue_cooldown_hours * 3600
            if time.time() - existing["updated_at"] < cooldown:
                return f"issue {issue_id} in cooldown ({existing['status']})"
        if self.store.runs_today() >= self.settings.max_runs_per_day:
            return f"daily cap of {self.settings.max_runs_per_day} runs reached; skipping {issue_id}"

        self.store.upsert(issue_id, project, title, "queued")
        self.queue.put_nowait(issue_id)
        return f"issue {issue_id} queued"

    async def run_forever(self):
        log.info("worker started")
        while True:
            issue_id = await self.queue.get()
            try:
                await self._process(issue_id)
            except Exception as e:
                log.exception("job %s failed", issue_id)
                self.store.set_status(issue_id, "error", detail=str(e)[:2000])
            finally:
                self.queue.task_done()

    async def _process(self, issue_id: str):
        self.store.set_status(issue_id, "running")
        issue = await self.sentry.issue(issue_id)
        event = await self.sentry.latest_event(issue_id)

        project_slug = (issue.get("project") or {}).get("slug", "")
        target = self.settings.repo_for_project(project_slug)
        if target is None:
            self.store.set_status(issue_id, "no_fix", detail=f"no repo mapped for project '{project_slug}'")
            return

        branch = f"brain/sentry-{issue_id}"
        workspace = await prepare_workspace(self.settings, target, branch)

        prompt = build_fix_prompt(
            repo=target.repo,
            base_branch=target.base,
            branch=branch,
            project_slug=project_slug,
            issue_id=issue_id,
            issue_title=issue.get("title", "unknown"),
            issue_url=issue.get("permalink", ""),
            culprit=issue.get("culprit", "unknown"),
            times_seen=str(issue.get("count", "?")),
            users_affected=str(issue.get("userCount", "?")),
            stacktrace=format_stacktrace(event),
        )

        log.info("running claude for issue %s (%s)", issue_id, target.repo)
        result = await run_claude(self.settings, workspace, prompt)
        self.store.set_status(issue_id, result.status, pr_url=result.pr_url, detail=result.detail)
        log.info("issue %s -> %s %s", issue_id, result.status, result.pr_url or "")

        if result.status == "pr_opened":
            await self.sentry.post_comment(
                issue_id, f"gumo_brain opened a draft PR for this issue: {result.pr_url}"
            )
        elif result.status == "no_fix" and result.detail:
            analysis = result.detail.split("NO_FIX:", 1)[-1].strip()[:1500]
            await self.sentry.post_comment(
                issue_id, f"gumo_brain investigated but did not open a PR:\n\n{analysis}"
            )

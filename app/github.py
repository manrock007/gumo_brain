"""GitHub integration — PR lifecycle actions for the PRs the engine opens.

All calls are best-effort, mirroring the ClickUp client: a GitHub outage
degrades PR shepherding, never the runs themselves. Uses the same
fine-grained PAT the headless runs push with (settings.github_token).

Why GraphQL for un-drafting: the REST v3 pulls API accepts `draft` only at
creation time — flipping an existing draft to ready-for-review is the
`markPullRequestReadyForReview` mutation, which needs the PR node id (fetched
via REST first).
"""

import logging

import httpx

from .config import Settings

log = logging.getLogger("brain.github")

API = "https://api.github.com"


class GitHub:
    def __init__(self, settings: Settings):
        self.enabled = bool(settings.github_token)
        self._headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def get_pr(self, repo: str, number: int) -> dict | None:
        """PR facts: draft, state, merged, node_id. None on any failure —
        callers must treat None as 'unknown', never as 'closed'."""
        if not self.enabled or not repo or not number:
            return None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{API}/repos/{repo}/pulls/{number}",
                                     headers=self._headers)
                if r.status_code != 200:
                    log.warning("get_pr %s#%s -> %s", repo, number, r.status_code)
                    return None
                return r.json()
        except Exception:
            log.exception("get_pr %s#%s failed", repo, number)
            return None

    async def mark_ready(self, repo: str, number: int) -> bool:
        """Flip a draft PR to ready-for-review. True on success or when the PR
        is already ready; False when it could not be done (stay draft-safe)."""
        if not self.enabled:
            return False
        pr = await self.get_pr(repo, number)
        if pr is None:
            return False
        if not pr.get("draft"):
            return True  # already ready — idempotent success
        node_id = pr.get("node_id") or ""
        if not node_id:
            return False
        query = ("mutation($id: ID!) { markPullRequestReadyForReview("
                 "input: {pullRequestId: $id}) { pullRequest { isDraft } } }")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"{API}/graphql", headers=self._headers,
                                      json={"query": query, "variables": {"id": node_id}})
                ok = r.status_code == 200 and not (r.json().get("errors"))
                if not ok:
                    log.warning("mark_ready %s#%s -> %s %s", repo, number,
                                r.status_code, str(r.text)[:300])
                return ok
        except Exception:
            log.exception("mark_ready %s#%s failed", repo, number)
            return False

    async def comment(self, repo: str, number: int, body: str) -> bool:
        """Post an issue comment on the PR (this is how '@sentry review' is
        triggered — review-thread replies alone do not re-engage the bot)."""
        if not self.enabled or not repo or not number:
            return False
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"{API}/repos/{repo}/issues/{number}/comments",
                                      headers=self._headers, json={"body": body})
                if r.status_code != 201:
                    log.warning("comment %s#%s -> %s", repo, number, r.status_code)
                return r.status_code == 201
        except Exception:
            log.exception("comment %s#%s failed", repo, number)
            return False

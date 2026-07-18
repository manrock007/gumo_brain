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
from .vcs import VCS

log = logging.getLogger("brain.github")

API = "https://api.github.com"
MAX_PAGES = 10  # 1000 items — far beyond any sane PR; bounds a pathological one


class GitHubVCS(VCS):
    """The DEFAULT VCS driver (Epic H2) — PR lifecycle + clone/token minting.
    Conforms to the ``VCS`` ABC; the PR methods below are unchanged HTTP logic,
    the seam adds only ``clone_url`` and ``mint_git_token`` (moved out of
    fixer)."""

    name = "github"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.enabled = bool(settings.github_token)
        self._settings = settings
        self._transport = transport  # test seam — None means real HTTP
        self._headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=30, transport=self._transport)

    def clone_url(self, repo: str) -> str:
        """The HTTPS clone URL for a repo — today's hardcoded fixer value."""
        return f"https://github.com/{repo}.git"

    async def mint_git_token(self, repo: str) -> str | None:
        """Resolve the GH_TOKEN a run for `repo` should carry (Epic G1). Returns
        an app installation token when the GitHub App covers the repo, else None
        so the caller lets the PAT env default apply. Never raises — app errors
        fall back to the PAT. Semantics are exactly the former
        ``fixer.mint_git_token``: gated on ``github_app_enabled`` and only the
        ``kind=='app'`` token is returned."""
        if not getattr(self._settings, "github_app_enabled", False):
            return None
        from .githubapp import effective_git_token
        token, kind = await effective_git_token(self._settings, repo)
        return token if kind == "app" else None

    async def _get_paged(self, path: str) -> list[dict] | None:
        """GET a list endpoint following Link rel=next — page 1 alone holds the
        OLDEST 100 items, so an unpaginated read misses the newest trigger
        comment / findings on any chatty PR. None on any failure (a partial
        list would silently hide items — treat as unknown instead)."""
        out: list[dict] = []
        url: str | None = f"{API}{path}"
        params: dict | None = {"per_page": 100}
        try:
            async with self._client() as client:
                for _ in range(MAX_PAGES):
                    r = await client.get(url, headers=self._headers, params=params)
                    if r.status_code != 200:
                        log.warning("GET %s -> %s", path, r.status_code)
                        return None
                    out.extend(r.json())
                    url = (r.links.get("next") or {}).get("url")
                    if not url:
                        return out
                    params = None  # the next-URL carries its own query string
            log.warning("GET %s: pagination cap (%d pages) hit", path, MAX_PAGES)
            return out
        except Exception:
            log.exception("GET %s failed", path)
            return None

    async def get_pr(self, repo: str, number: int) -> dict | None:
        """PR facts: draft, state, merged, node_id. None on any failure —
        callers must treat None as 'unknown', never as 'closed'."""
        if not self.enabled or not repo or not number:
            return None
        try:
            async with self._client() as client:
                r = await client.get(f"{API}/repos/{repo}/pulls/{number}",
                                     headers=self._headers)
                if r.status_code != 200:
                    log.warning("get_pr %s#%s -> %s", repo, number, r.status_code)
                    return None
                return r.json()
        except Exception:
            log.exception("get_pr %s#%s failed", repo, number)
            return None

    async def create_pr(self, repo: str, head: str, base: str, title: str,
                        body: str, draft: bool = True) -> str | None:
        """Open a PR (draft by default) — the outcome loop's mechanical
        memory-propagation PRs use this. Returns the PR's html_url, or None on
        any failure (best-effort like everything here)."""
        if not self.enabled or not repo or not head or not base:
            return None
        try:
            async with self._client() as client:
                r = await client.post(
                    f"{API}/repos/{repo}/pulls", headers=self._headers,
                    json={"title": title[:250], "head": head, "base": base,
                          "body": body[:60000], "draft": draft})
                if r.status_code != 201:
                    log.warning("create_pr %s %s->%s -> %s %s", repo, head, base,
                                r.status_code, str(r.text)[:300])
                    return None
                return (r.json().get("html_url") or "").strip() or None
        except Exception:
            log.exception("create_pr %s %s->%s failed", repo, head, base)
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
            async with self._client() as client:
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

    async def list_comments(self, repo: str, number: int) -> list[dict] | None:
        """ALL issue comments on the PR, oldest first (where '@sentry review'
        triggers live — the newest matters, hence full pagination). None on
        failure — never a partial list."""
        if not self.enabled or not repo or not number:
            return None
        return await self._get_paged(f"/repos/{repo}/issues/{number}/comments")

    async def get_comment_reactions(self, repo: str, comment_id: int) -> list[dict] | None:
        """Reactions on an issue comment — a 🎉 ('hooray') on the latest
        '@sentry review' trigger is the bot's clean-pass signal."""
        if not self.enabled or not repo or not comment_id:
            return None
        return await self._get_paged(f"/repos/{repo}/issues/comments/{comment_id}/reactions")

    async def get_review_comments(self, repo: str, number: int) -> list[dict] | None:
        """ALL line-level review comments (where the bot's findings live),
        oldest first, fully paginated. None on failure."""
        if not self.enabled or not repo or not number:
            return None
        return await self._get_paged(f"/repos/{repo}/pulls/{number}/comments")

    async def reply_to_review_comment(self, repo: str, number: int,
                                      comment_id: int, body: str) -> bool:
        if not self.enabled or not repo or not number or not comment_id:
            return False
        try:
            async with self._client() as client:
                r = await client.post(
                    f"{API}/repos/{repo}/pulls/{number}/comments/{comment_id}/replies",
                    headers=self._headers, json={"body": body})
                if r.status_code != 201:
                    log.warning("reply %s#%s/%s -> %s", repo, number, comment_id, r.status_code)
                return r.status_code == 201
        except Exception:
            log.exception("reply %s#%s/%s failed", repo, number, comment_id)
            return False

    async def comment(self, repo: str, number: int, body: str) -> bool:
        """Post an issue comment on the PR (this is how '@sentry review' is
        triggered — review-thread replies alone do not re-engage the bot)."""
        if not self.enabled or not repo or not number:
            return False
        try:
            async with self._client() as client:
                r = await client.post(f"{API}/repos/{repo}/issues/{number}/comments",
                                      headers=self._headers, json={"body": body})
                if r.status_code != 201:
                    log.warning("comment %s#%s -> %s", repo, number, r.status_code)
                return r.status_code == 201
        except Exception:
            log.exception("comment %s#%s failed", repo, number)
            return False


# Back-compat alias — engine.py imports `GitHub`; test_shepherd/test_prs
# construct it directly with transport=. Load-bearing, not cosmetic.
GitHub = GitHubVCS

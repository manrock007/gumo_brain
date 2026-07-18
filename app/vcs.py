"""VCS adapter (Epic H2, SCAFFOLD): the version-control / PR-host seam.

One interface, drivers behind it. GitHub is the current (and default) driver —
it IS today's behavior byte-for-byte (see ``app/github.py``): PR lifecycle,
clone-URL minting, and per-repo installation-token minting (Epic G1). A GitLab
driver is stubbed here as an inert no-op so a deployment that flips
``VCS_PROVIDER=gitlab`` degrades to PAT-only / dashboard-only PR shepherding
exactly like a GitHub-off instance — it NEVER raises into control flow (the §7
best-effort invariant). Mapping notes: ``docs/VCS-GITLAB.md``.

Two responsibilities (BUILD-PLAN H2):
- Repo plumbing: ``clone_url(repo)`` and ``mint_git_token(repo)``.
- PR lifecycle: get / create / mark-ready / comment / reply / list-comments /
  review-comments / reactions.

The seam follows the established pattern (``app/analytics.py`` /
``app/secrets.py``): an ABC with ``name`` + ``enabled``, concrete drivers, a
``VCS_PROVIDERS`` allow-list, and a fail-closed ``vcs_for(settings)`` factory
returning the DEFAULT (GitHub) driver for an empty or unknown name — never a
null VCS (there is no null steady state; a GitHub-off box still runs the GitHub
driver with ``enabled=False``).

NOTE on the seam being "on the call path": after this SCAFFOLD the engine still
reaches the VCS through the single ``self.github`` attribute (retyped to
``VCS``), constructed once via ``vcs_for``; fixer resolves a VCS internally
once per workspace prep. The factory is exercised at those construction points,
not per PR call.
"""

import logging
from abc import ABC, abstractmethod

import httpx

log = logging.getLogger("brain.vcs")

# No empty-string member: an unconfigured box still runs the GitHub driver
# (enabled=False when the token is absent). '' → the default driver.
VCS_PROVIDERS = ("github", "gitlab")


class VCS(ABC):
    """The H2 seam. Drivers are best-effort and NEVER raise into control flow.

    ``enabled`` mirrors ``GitHub.enabled`` — worker.py reads
    ``self.engine.github.enabled`` to decide whether the PR shepherd runs.
    """

    name = "base"
    enabled = False

    # --- repo plumbing ---
    @abstractmethod
    def clone_url(self, repo: str) -> str: ...

    @abstractmethod
    async def mint_git_token(self, repo: str) -> str | None: ...

    # --- PR lifecycle ---
    @abstractmethod
    async def get_pr(self, repo: str, number: int) -> dict | None: ...

    @abstractmethod
    async def create_pr(self, repo: str, head: str, base: str, title: str,
                        body: str, draft: bool = True) -> str | None: ...

    @abstractmethod
    async def mark_ready(self, repo: str, number: int) -> bool: ...

    @abstractmethod
    async def comment(self, repo: str, number: int, body: str) -> bool: ...

    @abstractmethod
    async def reply_to_review_comment(self, repo: str, number: int,
                                      comment_id: int, body: str) -> bool: ...

    @abstractmethod
    async def list_comments(self, repo: str, number: int) -> list[dict] | None: ...

    @abstractmethod
    async def get_review_comments(self, repo: str, number: int) -> list[dict] | None: ...

    @abstractmethod
    async def get_comment_reactions(self, repo: str, comment_id: int) -> list[dict] | None: ...


class GitLabVCS(VCS):
    """SCAFFOLD driver — inert. PR ops are logged no-ops returning
    ``None``/``False``; ``clone_url`` still returns a well-formed HTTPS URL
    (clone is provider-agnostic) so even the stub doesn't wedge a checkout;
    ``mint_git_token`` returns None (PAT fallback). A real driver replaces these
    with GitLab MR calls per ``docs/VCS-GITLAB.md`` and flips ``enabled``."""

    name = "gitlab"
    enabled = False

    def __init__(self, settings):
        self.settings = settings
        log.info("GitLab VCS is a scaffold (enabled=False) — PR shepherding "
                 "degrades to dashboard/PAT-only. See docs/VCS-GITLAB.md")

    def clone_url(self, repo: str) -> str:
        return f"https://gitlab.com/{repo}.git"

    async def mint_git_token(self, repo: str) -> str | None:
        return None

    async def get_pr(self, repo: str, number: int) -> dict | None:
        return None

    async def create_pr(self, repo: str, head: str, base: str, title: str,
                        body: str, draft: bool = True) -> str | None:
        return None

    async def mark_ready(self, repo: str, number: int) -> bool:
        return False

    async def comment(self, repo: str, number: int, body: str) -> bool:
        return False

    async def reply_to_review_comment(self, repo: str, number: int,
                                      comment_id: int, body: str) -> bool:
        return False

    async def list_comments(self, repo: str, number: int) -> list[dict] | None:
        return None

    async def get_review_comments(self, repo: str, number: int) -> list[dict] | None:
        return None

    async def get_comment_reactions(self, repo: str, comment_id: int) -> list[dict] | None:
        return None


def vcs_for(settings, transport: httpx.AsyncBaseTransport | None = None) -> VCS:
    """Resolve the VCS driver from ``settings.vcs_provider``. Fail closed to the
    working DEFAULT (GitHub) for an empty or unknown name — never a null VCS
    that would silently disable GitHub on a zero-config box. ``transport`` is the
    GitHub test seam (ignored by the stub)."""
    # deferred import avoids a github<->vcs module cycle (github imports VCS
    # from here at module load).
    from .github import GitHubVCS

    provider = (getattr(settings, "vcs_provider", "") or "").strip().lower()
    if provider == "gitlab":
        return GitLabVCS(settings)
    if provider not in ("", "github"):
        log.warning("unknown VCS_PROVIDER=%r — using github", provider[:40])
    return GitHubVCS(settings, transport=transport)

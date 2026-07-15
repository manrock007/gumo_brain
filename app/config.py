import json
import os
from functools import lru_cache

from pydantic_settings import BaseSettings


class RepoTarget:
    def __init__(self, repo: str, base: str, setup_cmd: str | None = None,
                 test_cmd: str | None = None, allow: list[str] | None = None):
        self.repo = repo          # e.g. "manrock007/gumoserver"
        self.base = base          # PR base branch, e.g. "master"
        self.setup_cmd = setup_cmd  # run once to install test deps, e.g. "npm ci"
        self.test_cmd = test_cmd    # how Claude should run the unit tests
        self.allow = allow or []    # extra --allowedTools entries for this repo


DEFAULT_REPO_MAP = {
    "gumo": {
        "repo": "manrock007/gumoserver", "base": "master",
        # Django tests need postgres/GDAL — not runnable in this container yet
        "setup_cmd": None, "test_cmd": None, "allow": [],
    },
    "web": {
        "repo": "manrock007/gumowebclient", "base": "dev",
        "setup_cmd": "npm ci", "test_cmd": "npm test",
        "allow": ["Bash(npm:*)", "Bash(npx:*)", "Bash(node:*)"],
    },
    "react-native": {
        "repo": "manrock007/gumoclient", "base": "master",
        "setup_cmd": "cd codebase/Gumo && npm ci",
        "test_cmd": "cd codebase/Gumo && npx jest --ci",
        "allow": ["Bash(npm:*)", "Bash(npx:*)", "Bash(node:*)", "Bash(cd:*)"],
    },
    "gumo-video-analyser": {
        "repo": "manrock007/gumo_video_analyser", "base": "main",
        "setup_cmd": None, "test_cmd": None, "allow": [],
    },
}


class Settings(BaseSettings):
    # Sentry
    sentry_org: str = "gumo"
    # EU-region org — do NOT use https://sentry.io here
    sentry_api_base: str = "https://de.sentry.io/api/0"
    sentry_web_base: str = "https://gumo.sentry.io"
    # Client secret of the Sentry internal integration (verifies webhook signatures)
    sentry_client_secret: str = ""
    # Auth token of the same internal integration (reads issues, posts comments)
    sentry_auth_token: str = ""

    # GitHub token used for git push + `gh pr create` (fine-grained PAT)
    github_token: str = ""
    # PR lifecycle: auto-flip captured draft PRs to ready-for-review and post
    # the first `@sentry review` trigger (the Seer bot ignores plain pushes)
    pr_auto_ready: bool = True
    # The shepherd: autonomously drive tracked PRs through Sentry review —
    # fix findings, reply, re-trigger — until approved (or the round cap).
    shepherd_enabled: bool = True
    shepherd_interval_seconds: int = 180
    pr_max_review_rounds: int = 6

    # ClickUp — one task per issue being fixed; Claude posts progress comments
    clickup_token: str = ""
    clickup_list_id: str = "901615853762"  # "Sentry Autofix" list in Gumo Space
    clickup_poll_seconds: int = 120  # how often to check awaiting-input tickets

    # Dashboard basic auth (user "gumo"); dashboard + trigger disabled if empty
    dashboard_password: str = ""

    # Sentry project slug -> repo config as a JSON string
    repo_map: str = json.dumps(DEFAULT_REPO_MAP)

    # Also react to plain "issue created" webhooks (very noisy — off by default;
    # alert rules firing `event_alert` webhooks are the intended trigger).
    handle_new_issues: bool = False

    # Grading — issues below min score (or resolved/ignored/stale) never reach Claude
    grade_min_score: int = 40
    grade_stale_days: int = 90  # last seen older than this -> stale, skip

    # Periodic sweep of top unresolved issues (catches legacy items that
    # predate the webhook or fired while the daily cap was exhausted)
    sweep_enabled: bool = True
    sweep_interval_hours: int = 24
    sweep_top_n: int = 3

    # Guardrails
    max_runs_per_day: int = 8
    issue_cooldown_hours: int = 72
    claude_timeout_seconds: int = 2400
    claude_model: str = ""  # empty -> CLI default
    claude_binary: str = "claude"

    # Feature pipeline (docs/ENGINE.md)
    doc_stage_timeout_seconds: int = 900   # P0-P4 are document-only runs
    clickup_mirror_max_chars: int = 50000  # artifact size above this -> pointer mirror
    memory_canonical_project: str = "gumo"  # repo hosting .gumo/product (product scope)
    stage_gates: str = ""  # future: comma list of auto-advance stages, e.g. "5,7"; empty = all gated
    reaper_grace_seconds: int = 600

    # Conversational gates (docs/CONVERSATIONS.md)
    chat_timeout_seconds: int = 300
    chat_max_turns_per_gate: int = 10
    # Session persistence (increment 2) — relocating the CLI config dir to the data
    # volume is a deploy-affecting substrate change (auth + onboarding state live
    # there). The bootstrap in fixer.ensure_session_store seeds it from the legacy
    # location; auth via CLAUDE_CODE_OAUTH_TOKEN/ANTHROPIC_API_KEY env passes through
    # regardless. OFF by default: without it, STAGE_ASK answers and code-gate chat
    # degrade to fresh runs (labeled), and sessions die with the container.
    session_persistence: bool = False
    session_ttl_days: int = 14
    max_asks_per_stage: int = 3         # STAGE_ASK resumes per (job, stage, attempt)
    default_gate_mode: str = "full"     # full = every stage parks | light = P0/P1/P3/P9 + guards

    # Two-lane gate chat (docs/CONVERSATIONS.md §5). The fast lane answers from
    # the gate bundle via a direct streaming API call (first tokens ~1-2s) and
    # escalates itself to the tool-run slow lane when the question needs the
    # repository. OFF unless chat_fast_model is set AND a key is available; the
    # key comes from CHAT_API_KEY (its own env var, falling back to
    # ANTHROPIC_API_KEY) and is used ONLY for fast-lane HTTP calls — CLI runs
    # keep their own auth untouched.
    chat_fast_model: str = ""           # e.g. "claude-sonnet-5"; empty = disabled
    chat_api_key: str = ""
    chat_api_base: str = "https://api.anthropic.com"
    chat_fast_timeout_seconds: int = 90
    chat_fast_max_tokens: int = 1500

    @property
    def effective_chat_api_key(self) -> str:
        return self.chat_api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    @property
    def chat_fast_enabled(self) -> bool:
        return bool(self.chat_fast_model and self.effective_chat_api_key)

    @property
    def claude_config_dir(self) -> str:
        """Session transcripts live on the data volume so resume survives restarts."""
        return f"{self.data_dir}/claude-config"

    @property
    def claude_chat_config_dir(self) -> str:
        """Artifact-primed chats get their own config dir so concurrent invocations
        never race the session store's .claude.json (docs/CONVERSATIONS.md §4)."""
        return f"{self.data_dir}/claude-config-chat"

    # Storage
    data_dir: str = "/data"

    @property
    def db_path(self) -> str:
        return f"{self.data_dir}/brain.db"

    @property
    def workspaces_dir(self) -> str:
        return f"{self.data_dir}/workspaces"

    @property
    def clickup_enabled(self) -> bool:
        return bool(self.clickup_token and self.clickup_list_id)

    def target_for_repo(self, repo: str) -> "RepoTarget | None":
        """Reverse lookup for the shepherd: a prs row carries owner/name, not a
        project slug."""
        for slug in json.loads(self.repo_map):
            t = self.repo_for_project(slug)
            if t and t.repo == repo:
                return t
        return None

    def repo_for_project(self, project_slug: str) -> RepoTarget | None:
        mapping = json.loads(self.repo_map)
        entry = mapping.get(project_slug)
        if not entry:
            return None
        return RepoTarget(
            repo=entry["repo"],
            base=entry.get("base", "main"),
            setup_cmd=entry.get("setup_cmd"),
            test_cmd=entry.get("test_cmd"),
            allow=entry.get("allow") or [],
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()

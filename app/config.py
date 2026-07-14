import json
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

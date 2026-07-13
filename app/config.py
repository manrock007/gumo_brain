import json
from functools import lru_cache

from pydantic_settings import BaseSettings


class RepoTarget:
    def __init__(self, repo: str, base: str):
        self.repo = repo  # e.g. "manrock007/gumoserver"
        self.base = base  # PR base branch, e.g. "master"


class Settings(BaseSettings):
    # Sentry
    sentry_org: str = "gumo"
    # EU-region org — do NOT use https://sentry.io here
    sentry_api_base: str = "https://de.sentry.io/api/0"
    # Client secret of the Sentry internal integration (verifies webhook signatures)
    sentry_client_secret: str = ""
    # Auth token of the same internal integration (reads issues, posts comments)
    sentry_auth_token: str = ""

    # GitHub token used for git push + `gh pr create` (fine-grained PAT)
    github_token: str = ""

    # Sentry project slug -> {"repo": "...", "base": "..."} as a JSON string
    repo_map: str = json.dumps({
        "gumo": {"repo": "manrock007/gumoserver", "base": "master"},
        "web": {"repo": "manrock007/gumowebclient", "base": "dev"},
        "react-native": {"repo": "manrock007/gumoclient", "base": "master"},
        "gumo-video-analyser": {"repo": "manrock007/gumo_video_analyser", "base": "main"},
    })

    # Also react to plain "issue created" webhooks (very noisy — off by default;
    # alert rules firing `event_alert` webhooks are the intended trigger).
    handle_new_issues: bool = False

    # Guardrails
    max_runs_per_day: int = 8
    issue_cooldown_hours: int = 72
    claude_timeout_seconds: int = 2400
    claude_model: str = ""  # empty -> CLI default
    claude_binary: str = "claude"

    # Storage
    data_dir: str = "/data"

    @property
    def db_path(self) -> str:
        return f"{self.data_dir}/brain.db"

    @property
    def workspaces_dir(self) -> str:
        return f"{self.data_dir}/workspaces"

    def repo_for_project(self, project_slug: str) -> RepoTarget | None:
        mapping = json.loads(self.repo_map)
        entry = mapping.get(project_slug)
        if not entry:
            return None
        return RepoTarget(repo=entry["repo"], base=entry.get("base", "main"))


@lru_cache
def get_settings() -> Settings:
    return Settings()

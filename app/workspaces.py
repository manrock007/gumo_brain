"""Workspaces (docs/ENGINE.md §12): Business → Workspace → Repo.

A workspace is a product surface (App, Dashboard, …) owning its repos, its
canonical repo for product-scope memory, its product name, its context text,
and its optional ClickUp list / Slack webhook. Project slugs are UNIQUE
ACROSS ALL WORKSPACES (DB index), so slug-keyed resolution — Sentry webhook
routing, job dispatch, the shepherd — stays deterministic and the historical
`settings.repo_for_project(slug)` contract keeps working unchanged: after
every workspace edit the service rebuilds the merged repo map into the live
Settings (same mechanism as the §10 runtime overrides).

Migration: a deployment upgrading from the single-context era gets a default
workspace built from its effective settings; existing jobs and all existing
users are attached to it.
"""

import json
import logging
import re
import sqlite3

from .config import Settings, validate_repo_map
from .db import JobStore

log = logging.getLogger("brain.workspaces")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

WORKSPACE_FIELDS = ("name", "product_name", "workspace_context", "canonical_project",
                    "clickup_list_id", "clickup_enabled", "slack_webhook_url",
                    "gate_mode_default")

WORKSPACE_CONTEXT_CAP = 4000


class WorkspaceError(ValueError):
    """Validation failure — safe to surface as a 400."""


class WorkspaceService:
    def __init__(self, store: JobStore, settings: Settings):
        self.store = store
        self.settings = settings

    # ---------- migration ----------

    def ensure_default(self):
        """First boot after the upgrade: wrap the effective §10 context into a
        default workspace, attach existing jobs and users. Idempotent."""
        if self.store.workspace_list():
            self.sync_settings()
            return
        mapping = json.loads(self.settings.repo_map)
        ws = self.store.workspace_create(
            "default", self.settings.product_name or "Default",
            product_name=self.settings.product_name,
            workspace_context="",  # business_context stays instance-level (§10)
            canonical_project=self.settings.memory_canonical_project,
            clickup_list_id=self.settings.clickup_list_id,
            clickup_enabled=int(bool(self.settings.clickup_token and self.settings.clickup_list_id)),
        )
        self.store.workspace_repos_replace(ws["id"], [
            {"slug": slug, **entry} for slug, entry in mapping.items()
        ])
        self.store.jobs_adopt_workspace(ws["id"])
        for u in self.store.user_list():
            self.store.workspace_member_set(ws["id"], u["id"], True)
        log.info("created default workspace '%s' with %d repos; adopted existing jobs/users",
                 ws["slug"], len(mapping))
        self.sync_settings()

    # ---------- settings compatibility ----------

    def sync_settings(self):
        """Rebuild the merged slug→target map into live Settings so every
        existing `repo_for_project` call site keeps working unchanged."""
        merged = {}
        for r in self.store.repo_rows_all():
            merged[r["slug"]] = {
                "repo": r["repo"], "base": r["base"],
                "setup_cmd": r["setup_cmd"], "test_cmd": r["test_cmd"],
                "allow": json.loads(r["allow"] or "[]"),
            }
        if merged:
            self.settings.repo_map = json.dumps(merged)

    # ---------- resolution ----------

    def for_project(self, project_slug: str) -> dict | None:
        """The workspace owning a project slug (canonical memory, ClickUp list,
        Slack nudges, membership checks all key off this)."""
        return self.store.workspace_for_slug(project_slug)

    def for_job(self, job: dict) -> dict | None:
        ws_id = job.get("workspace_id")
        if ws_id:
            ws = self.store.workspace_get(int(ws_id))
            if ws:
                return ws
        return self.for_project(job.get("project") or "")

    def user_can_access(self, user: dict, workspace_id: int | None) -> bool:
        """Admins see everything; members only assigned workspaces. Jobs with
        no resolvable workspace stay admin-only rather than leaking."""
        if user.get("role") == "admin":
            return True
        if workspace_id is None:
            return False
        return workspace_id in self.store.workspace_ids_for_user(user["id"])

    def user_workspaces(self, user: dict) -> list[dict]:
        all_ws = self.store.workspace_list()
        if user.get("role") == "admin":
            return all_ws
        allowed = self.store.workspace_ids_for_user(user["id"])
        return [w for w in all_ws if w["id"] in allowed]

    # ---------- prompt briefing (§10/§12 hierarchy) ----------

    def briefing(self, ws: dict | None) -> str:
        """Business context (instance) + workspace context, stacked for the
        prompt's business block. Repo-level memory rides in the clone as ever."""
        parts = [self.settings.business_context.strip()]
        if ws and (ws.get("workspace_context") or "").strip():
            parts.append(f"### Workspace: {ws['name']}\n\n{ws['workspace_context'].strip()}")
        return "\n\n".join(p for p in parts if p)

    def briefing_for_job(self, job: dict) -> str:
        return self.briefing(self.for_job(job))

    def product_name_for(self, ws: dict | None) -> str:
        return (ws or {}).get("product_name") or self.settings.product_name

    def canonical_for(self, project_slug: str) -> str:
        """The canonical repo slug for a project — its workspace's, falling
        back to the instance default for unmapped slugs."""
        ws = self.for_project(project_slug)
        return (ws or {}).get("canonical_project") or self.settings.memory_canonical_project

    # ---------- integrations (§12: ClickUp optional, Slack nudges) ----------

    def clickup_route(self, project_slug: str) -> tuple[bool, str | None]:
        """(enabled, list_id) for tickets on this project's workspace. Falls
        back to instance settings for unmapped slugs (legacy behavior)."""
        ws = self.for_project(project_slug)
        if ws is None:
            return bool(self.settings.clickup_token and self.settings.clickup_list_id), None
        return bool(self.settings.clickup_token and ws["clickup_enabled"]), \
            (ws["clickup_list_id"] or None)

    async def notify_gate(self, job: dict, text: str):
        """Best-effort Slack nudge at gate park — the dashboard-only nudge
        channel for workspaces without ClickUp (and extra signal with it)."""
        ws = self.for_job(job)
        url = (ws or {}).get("slack_webhook_url") or ""
        if not url:
            return
        link = f"{self.settings.public_base_url}/#/job/{job.get('issue_id')}"
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, json={"text": f"{text}\n{link}"})
        except Exception:  # visibility only — never let a nudge break a gate
            log.warning("slack gate nudge failed for %s (non-fatal)", job.get("issue_id"))

    # ---------- validated writes ----------

    def create(self, slug: str, name: str, **fields) -> dict:
        slug = (slug or "").strip().lower()
        if not SLUG_RE.match(slug):
            raise WorkspaceError("workspace slug: 1-32 chars, a-z 0-9 and dashes")
        if self.store.workspace_get_by_slug(slug):
            raise WorkspaceError(f"workspace '{slug}' already exists")
        clean = self._clean_fields(fields)
        ws = self.store.workspace_create(slug, (name or slug).strip(), **clean)
        self.sync_settings()
        return ws

    def update(self, workspace_id: int, *, repos: list[dict] | None = None,
               **fields) -> dict:
        ws = self.store.workspace_get(int(workspace_id))
        if ws is None:
            raise WorkspaceError("unknown workspace")
        clean = self._clean_fields(fields)
        if repos is not None:
            mapping = validate_repo_map({r.get("slug"): r for r in repos})
            canonical = clean.get("canonical_project", ws["canonical_project"])
            if canonical and canonical not in mapping:
                raise WorkspaceError(
                    f"canonical project '{canonical}' is not a repo slug in this workspace")
            try:
                self.store.workspace_repos_replace(
                    ws["id"], [{"slug": s, **e} for s, e in mapping.items()])
            except sqlite3.IntegrityError:
                raise WorkspaceError(
                    "a project slug is already used by another workspace — slugs are global")
        elif "canonical_project" in clean and clean["canonical_project"]:
            own = {r["slug"] for r in self.store.workspace_repos_for(ws["id"])}
            if clean["canonical_project"] not in own:
                raise WorkspaceError(
                    f"canonical project '{clean['canonical_project']}' is not a repo slug in this workspace")
        if clean:
            self.store.workspace_set(ws["id"], **clean)
        self.sync_settings()
        return self.store.workspace_get(ws["id"])

    @staticmethod
    def _clean_fields(fields: dict) -> dict:
        clean = {}
        for key, value in fields.items():
            if key not in WORKSPACE_FIELDS or value is None:
                continue
            if key == "clickup_enabled":
                clean[key] = int(bool(value))
            elif key == "gate_mode_default":
                if value not in ("full", "light"):
                    raise WorkspaceError("gate_mode_default must be 'full' or 'light'")
                clean[key] = value
            else:
                value = str(value).strip()
                if key == "workspace_context" and len(value) > WORKSPACE_CONTEXT_CAP:
                    raise WorkspaceError(
                        f"workspace_context is capped at {WORKSPACE_CONTEXT_CAP} chars")
                clean[key] = value
        return clean

    def public(self, ws: dict) -> dict:
        """API/dashboard shape, repos + members inlined."""
        return {
            "id": ws["id"], "slug": ws["slug"], "name": ws["name"],
            "product_name": ws["product_name"],
            "workspace_context": ws["workspace_context"],
            "canonical_project": ws["canonical_project"],
            "clickup_list_id": ws["clickup_list_id"],
            "clickup_enabled": bool(ws["clickup_enabled"]),
            "slack_webhook_url": ws["slack_webhook_url"],
            "gate_mode_default": ws["gate_mode_default"],
            "repos": {r["slug"]: {"repo": r["repo"], "base": r["base"],
                                  "setup_cmd": r["setup_cmd"], "test_cmd": r["test_cmd"],
                                  "allow": json.loads(r["allow"] or "[]")}
                      for r in self.store.workspace_repos_for(ws["id"])},
            "members": self.store.workspace_members_get(ws["id"]),
        }

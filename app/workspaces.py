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

from . import analytics, roles
from .config import DEFAULT_PRODUCT_NAME, Settings, validate_repo_map, validate_stage_role_map
from .db import JobStore

log = logging.getLogger("brain.workspaces")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

WORKSPACE_FIELDS = ("name", "product_name", "workspace_context", "canonical_project",
                    "clickup_list_id", "clickup_enabled", "slack_webhook_url",
                    "gate_mode_default", "require_attributed_answers",
                    "stage_role_map", "gate_sla_hours",
                    "analytics_provider", "analytics_config",
                    "slack_channels", "budget_monthly_usd")

SLACK_CHANNELS_MAX = 50

WORKSPACE_CONTEXT_CAP = 4000


class WorkspaceError(ValueError):
    """Validation failure — safe to surface as a 400."""


class WorkspaceNotFound(WorkspaceError):
    """Unknown workspace — surfaces as a 404, matching the other endpoints."""


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
        # a still-default product name is NOT the operator's choice: name the
        # migrated workspace "Default" and store an EMPTY product_name so the
        # row falls through to the instance value (product_name_for) — persisting
        # the code default here would permanently shadow later PUT /api/context
        # edits in every prompt
        configured = (self.settings.product_name or "").strip()
        if configured == DEFAULT_PRODUCT_NAME:
            configured = ""
        ws = self.store.migrate_default_workspace(
            "default", configured or "Default",
            fields=dict(
                product_name=configured,
                workspace_context="",  # business_context stays instance-level (§10)
                canonical_project=self.settings.memory_canonical_project,
                clickup_list_id=self.settings.clickup_list_id,
                clickup_enabled=int(bool(self.settings.clickup_token and self.settings.clickup_list_id)),
            ),
            repos=[{"slug": slug, **entry} for slug, entry in mapping.items()],
            user_ids=[u["id"] for u in self.store.user_list()],
        )
        log.info("created default workspace '%s' with %d repos; adopted existing jobs/users",
                 ws["slug"], len(mapping))
        self.sync_settings()

    # ---------- settings compatibility ----------

    def sync_settings(self):
        """Rebuild the merged slug→target map into live Settings so every
        existing `repo_for_project` call site keeps working unchanged. Assigned
        UNCONDITIONALLY: an empty result must clear the map too, or removed
        repos would linger dispatchable in memory (sentry finding 1595917 —
        not reachable via the API today, which refuses empty repo sets, but
        any future delete surface would hit it)."""
        merged = {}
        for r in self.store.repo_rows_all():
            merged[r["slug"]] = {
                "repo": r["repo"], "base": r["base"],
                "setup_cmd": r["setup_cmd"], "test_cmd": r["test_cmd"],
                "allow": json.loads(r["allow"] or "[]"),
            }
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
        """The canonical repo slug for a project. Strictly its own workspace's:
        a workspace without a canonical has NO product scope (memory degrades
        gracefully) rather than borrowing another workspace's product context
        via the instance fallback (sentry finding 1595794). The instance value
        applies only to legacy unmapped slugs, which never reach a run anyway."""
        ws = self.for_project(project_slug)
        if ws is not None:
            return ws.get("canonical_project") or ""
        return self.settings.memory_canonical_project

    # ---------- integrations (§12: ClickUp optional, Slack nudges) ----------

    def clickup_route(self, project_slug: str) -> tuple[bool, str | None]:
        """(enabled, list_id) for tickets on this project's workspace. Falls
        back to instance settings for unmapped slugs (legacy behavior).
        Enabled REQUIRES the workspace's own list id (validated at save;
        re-checked here so a legacy/hand-edited row can never silently route
        tickets into the instance-global list — sentry finding 1595595)."""
        ws = self.for_project(project_slug)
        if ws is None:
            return bool(self.settings.clickup_token and self.settings.clickup_list_id), None
        list_id = (ws["clickup_list_id"] or "").strip()
        return bool(self.settings.clickup_token and ws["clickup_enabled"] and list_id), \
            (list_id or None)

    async def notify_text(self, ws: dict | None, text: str):
        """Best-effort Slack send to a workspace's incoming webhook — never
        raises, never drives control flow (Epic I2 digests + gate nudges both
        funnel through here)."""
        url = (ws or {}).get("slack_webhook_url") or ""
        if not url:
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, json={"text": text})
        except Exception:  # visibility only — never let a send break anything
            log.warning("slack notify failed for workspace %s (non-fatal)",
                        (ws or {}).get("slug"))

    async def notify_gate(self, job: dict, text: str):
        """Best-effort Slack nudge at gate park — the dashboard-only nudge
        channel for workspaces without ClickUp (and extra signal with it)."""
        ws = self.for_job(job)
        link = ""  # deep link only when a public base is configured
        if self.settings.public_base_url:
            link = f"\n{self.settings.public_base_url}/#/job/{job.get('issue_id')}"
        await self.notify_text(ws, f"{text}{link}")

    # ---------- validated writes ----------

    def create(self, slug: str, name: str, **fields) -> dict:
        slug = (slug or "").strip().lower()
        if not SLUG_RE.match(slug):
            raise WorkspaceError("workspace slug: 1-32 chars, a-z 0-9 and dashes")
        if self.store.workspace_get_by_slug(slug):
            raise WorkspaceError(f"workspace '{slug}' already exists")
        clean = self._clean_fields(fields)
        if clean.get("clickup_enabled") and not str(clean.get("clickup_list_id") or "").strip():
            raise WorkspaceError(
                "ClickUp mirroring needs this workspace's list id — set it or disable mirroring")
        if "slack_channels" in clean:
            self._check_slack_channels_unique(clean["slack_channels"], None)
        ws = self.store.workspace_create(slug, (name or slug).strip(), **clean)
        if "slack_channels" in clean:
            self._init_slack_cursors(clean["slack_channels"])
        # Epic I1: every workspace gets its proactive-routine rows at birth.
        # Lazy import — routines must stay importable without this module.
        from . import routines
        routines.ensure_seeds_for_workspace(self.store, self.settings, ws["id"])
        self.sync_settings()
        return ws

    def update(self, workspace_id: int, *, repos: list[dict] | None = None,
               **fields) -> dict:
        ws = self.store.workspace_get(int(workspace_id))
        if ws is None:
            raise WorkspaceNotFound("unknown workspace")
        clean = self._clean_fields(fields)
        if repos is not None:
            try:
                mapping = validate_repo_map({r.get("slug"): r for r in repos})
            except ValueError as e:
                # plain ValueError would escape the endpoint's WorkspaceError
                # handler and 500 — same validation failure, same 400
                raise WorkspaceError(str(e))
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
        # co-validation against the MERGED state: enabling ClickUp without a
        # list id would silently fall back to the instance-global list
        if clean.get("clickup_enabled", ws["clickup_enabled"]) and \
                not str(clean.get("clickup_list_id", ws["clickup_list_id"]) or "").strip():
            raise WorkspaceError(
                "ClickUp mirroring needs this workspace's list id — set it or disable mirroring")
        if "slack_channels" in clean:
            # a channel routes candidates to exactly ONE workspace (same
            # determinism rationale as global repo slugs). Read-check only —
            # acceptable under today's single-process SQLite because the check
            # and the write share this synchronous section with NO await
            # between them; re-verify under Epic F2 (ENGINE.md §16 recorded
            # edges, same treatment as the auto-advance non-CAS note).
            self._check_slack_channels_unique(clean["slack_channels"], ws["id"])
        if clean:
            self.store.workspace_set(ws["id"], **clean)
        if "slack_channels" in clean:
            self._init_slack_cursors(clean["slack_channels"])
        self.sync_settings()
        return self.store.workspace_get(ws["id"])

    @staticmethod
    def slack_channels_of(ws: dict) -> list[str]:
        """Read-tolerant decode of a workspace row's channel allowlist."""
        raw = (ws.get("slack_channels") or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            return [str(c) for c in parsed if str(c).strip()] \
                if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []

    def _check_slack_channels_unique(self, channels_json: str, ws_id: int | None):
        wanted = set(json.loads(channels_json) if channels_json else [])
        if not wanted:
            return
        for other in self.store.workspace_list():
            if ws_id is not None and other["id"] == ws_id:
                continue
            clash = wanted & set(self.slack_channels_of(other))
            if clash:
                raise WorkspaceError(
                    f"Slack channel(s) {', '.join(sorted(clash))} already "
                    f"allowlisted by workspace '{other['slug']}' — a channel "
                    "routes to exactly one workspace")

    def _init_slack_cursors(self, channels_json: str):
        """Initialize NEW channels' watermarks to NOW (forward-only ingestion —
        blocker: the first pass after enabling must never flood the inbox with
        historical candidates). Existing watermarks are never moved."""
        import time
        for ch in (json.loads(channels_json) if channels_json else []):
            self.store.slack_cursor_init(ch, f"{time.time():.6f}")

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
            elif key == "require_attributed_answers":
                value = str(value).strip().lower()
                if value not in ("auto", "on", "off"):
                    raise WorkspaceError(
                        "require_attributed_answers must be 'auto', 'on' or 'off'")
                clean[key] = value
            elif key == "stage_role_map":
                # fail closed on the WRITE side: a malformed map is a 400 and
                # nothing changes; '' clears back to inherit
                if isinstance(value, str) and not value.strip():
                    clean[key] = ""
                    continue
                try:
                    mapping = json.loads(value) if isinstance(value, str) else value
                    clean[key] = json.dumps(validate_stage_role_map(mapping))
                except (ValueError, TypeError) as e:
                    raise WorkspaceError(f"stage_role_map: {e}")
            elif key == "analytics_provider":
                # allowed names come from the analytics registry — one list,
                # so the validator can't drift when the next driver lands
                value = str(value).strip().lower()
                if value not in analytics.ANALYTICS_PROVIDERS:
                    raise WorkspaceError(
                        "analytics_provider must be one of: "
                        + ", ".join(f"'{p}'" for p in analytics.ANALYTICS_PROVIDERS))
                clean[key] = value
            elif key == "analytics_config":
                # dict (API body) or JSON string; must be an object. Fail
                # closed: malformed -> 400, nothing changes. Stored verbatim
                # (SECRET AT REST) — public() never returns it.
                if isinstance(value, str) and not value.strip():
                    clean[key] = "{}"  # explicit clear
                    continue
                try:
                    parsed = json.loads(value) if isinstance(value, str) else value
                    if not isinstance(parsed, dict):
                        raise ValueError("must be a JSON object")
                except (ValueError, TypeError) as e:
                    raise WorkspaceError(f"analytics_config: {e}")
                clean[key] = json.dumps(parsed)
            elif key == "slack_channels":
                # Epic D3: list (API body) or JSON string of channel ids;
                # '' / [] clears. Fail closed: malformed -> 400, nothing changes.
                if isinstance(value, str) and not value.strip():
                    clean[key] = ""
                    continue
                try:
                    parsed = json.loads(value) if isinstance(value, str) else value
                    if not isinstance(parsed, list):
                        raise ValueError("must be a list of channel ids")
                    channels = []
                    for ch in parsed:
                        ch = str(ch or "").strip()
                        if not ch:
                            raise ValueError("channel ids must be non-empty strings")
                        if ch not in channels:
                            channels.append(ch)
                    if len(channels) > SLACK_CHANNELS_MAX:
                        raise ValueError(
                            f"at most {SLACK_CHANNELS_MAX} channels per workspace")
                except (ValueError, TypeError) as e:
                    raise WorkspaceError(f"slack_channels: {e}")
                clean[key] = json.dumps(channels) if channels else ""
            elif key == "budget_monthly_usd":
                # Epic I4 (Epic G4 extends): empty string -> NULL = inherit the
                # instance BUDGET_MONTHLY_USD; 0 = no budget. Fail closed on
                # anything unparseable or negative.
                if isinstance(value, str) and not value.strip():
                    clean[key] = None
                    continue
                try:
                    budget = float(value)
                except (ValueError, TypeError):
                    raise WorkspaceError("budget_monthly_usd must be a number ≥ 0")
                if budget < 0:
                    raise WorkspaceError("budget_monthly_usd must be a number ≥ 0")
                clean[key] = budget
            elif key == "gate_sla_hours":
                # empty string -> NULL = inherit the instance default
                if isinstance(value, str) and not value.strip():
                    clean[key] = None
                    continue
                try:
                    hours = int(value)
                except (ValueError, TypeError):
                    raise WorkspaceError("gate_sla_hours must be an integer ≥ 0")
                if hours < 0:
                    raise WorkspaceError("gate_sla_hours must be an integer ≥ 0")
                clean[key] = hours
            else:
                value = str(value).strip()
                # name is the one field that must never be blank; the OTHER
                # empty strings are meaningful clears (product_name -> instance
                # default, canonical_project -> no product scope, urls/lists off)
                if key == "name" and not value:
                    raise WorkspaceError("workspace name cannot be empty")
                if key == "workspace_context" and len(value) > WORKSPACE_CONTEXT_CAP:
                    raise WorkspaceError(
                        f"workspace_context is capped at {WORKSPACE_CONTEXT_CAP} chars")
                clean[key] = value
        return clean

    def analytics_for(self, ws: dict | None):
        """The workspace's analytics driver (Epic B3) — NullAnalytics when
        nothing is configured, so callers never branch on 'no analytics'."""
        return analytics.provider_for(self.settings, ws)

    @staticmethod
    def _analytics_configured(ws: dict) -> bool:
        """Configured-ness WITHOUT the secret: does the stored config carry any
        substantive key? The dashboard shows this boolean, never the config."""
        if not str(ws.get("analytics_provider") or "").strip():
            return False
        try:
            config = json.loads(ws.get("analytics_config") or "{}")
        except (ValueError, TypeError):
            return False
        return any(str(config.get(k) or "").strip()
                   for k in ("project_id", "service_account", "secret"))

    def public(self, ws: dict) -> dict:
        """API/dashboard shape, repos + members inlined. NEVER includes
        analytics_config — it holds the provider secret."""
        return {
            "id": ws["id"], "slug": ws["slug"], "name": ws["name"],
            "product_name": ws["product_name"],
            "workspace_context": ws["workspace_context"],
            "canonical_project": ws["canonical_project"],
            "clickup_list_id": ws["clickup_list_id"],
            "clickup_enabled": bool(ws["clickup_enabled"]),
            "slack_webhook_url": ws["slack_webhook_url"],
            "gate_mode_default": ws["gate_mode_default"],
            "require_attributed_answers": ws.get("require_attributed_answers") or "auto",
            "stage_role_map": ws.get("stage_role_map") or "",
            "gate_sla_hours": ws.get("gate_sla_hours"),
            "budget_monthly_usd": ws.get("budget_monthly_usd"),
            "analytics_provider": ws.get("analytics_provider") or "",
            "analytics_configured": self._analytics_configured(ws),
            # channel ids are not secrets (the bot token is, and lives in env)
            "slack_channels": self.slack_channels_of(ws),
            # the fully-merged 0–9 ownership ladder, so the UI shows effective
            # ownership without duplicating the merge logic client-side
            "stage_roles": {str(i): roles.role_for_stage(self.settings, ws, i)
                            for i in range(10)},
            "repos": {r["slug"]: {"repo": r["repo"], "base": r["base"],
                                  "setup_cmd": r["setup_cmd"], "test_cmd": r["test_cmd"],
                                  "allow": json.loads(r["allow"] or "[]")}
                      for r in self.store.workspace_repos_for(ws["id"])},
            "members": self.store.workspace_members_get(ws["id"]),
        }

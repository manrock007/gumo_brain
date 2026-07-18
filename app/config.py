import json
import logging
import os
import re
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings

log = logging.getLogger("brain.config")


class RepoTarget:
    def __init__(self, repo: str, base: str, setup_cmd: str | None = None,
                 test_cmd: str | None = None, allow: list[str] | None = None):
        self.repo = repo          # e.g. "acme/api"
        self.base = base          # PR base branch, e.g. "main"
        self.setup_cmd = setup_cmd  # run once to install test deps, e.g. "npm ci"
        self.test_cmd = test_cmd    # how Claude should run the unit tests
        self.allow = allow or []    # extra --allowedTools entries for this repo


# Neutral out of the box: a fresh install carries NO repos — the first-run
# wizard demands the first one via the workspace API. A concrete example map
# (the original Gumo instance) lives in docs/OPERATIONS.md.
DEFAULT_REPO_MAP: dict = {}

# The engine's own identity (the tool), distinct from product_name (what a
# team builds with it). Single source of truth for branding, gate-comment
# prefixes and the git author.
ENGINE_NAME = "CtrlLoop"
ENGINE_SLUG = "ctrlloop"

# The engine namespace inside customer repos (like `.github/`): artifacts,
# memory and product scope all live under it. Repos initialized before the
# rename keep their legacy tree working — ONE precedence rule governs every
# resolution helper (fixer.engine_dir, fixer.git_show_ns, the memory reads):
# legacy wins when present, so a repo is never split-brained across two trees.
# Migrate a repo with a single `git mv .gumo .ctrlloop` PR on the base branch
# (MIGRATION-CTRLLOOP.md).
ENGINE_DIR = ".ctrlloop"
LEGACY_ENGINE_DIRS = (".gumo",)
# Archival refs for rejected code-stage attempts: refs/<ns>/<job>/P<n>-attempt-<k>.
# Write-only (no read path), so no legacy fallback is needed.
REFS_NAMESPACE = "ctrlloop"

# Engine-authored ClickUp comments carry a fixed prefix the poller uses to
# ignore its own comments. Pre-rename comments still in threads must stay
# recognized, or parked jobs could misread them as human answers.
GATE_PREFIX = f"**[{ENGINE_SLUG}]**"
LEGACY_GATE_PREFIXES = ("**[gumo_brain]**",)
ENGINE_COMMENT_PREFIXES = (GATE_PREFIX, *LEGACY_GATE_PREFIXES)

DEFAULT_PRODUCT_NAME = "your product"

# Empty on purpose: any non-empty code default would be injected verbatim into
# EVERY run's prompt. Operators write their own via the dashboard's context
# panel (a template ships as its placeholder) or PUT /api/context; a worked
# example lives in docs/OPERATIONS.md.
DEFAULT_BUSINESS_CONTEXT = ""

# Project-context fields an operator may override at runtime (persisted in the
# app_config table, applied over env/code defaults at startup and via the API).
RUNTIME_CONTEXT_KEYS = ("product_name", "business_context", "repo_map",
                        "memory_canonical_project")

# Epic A3: built-in stage→role ownership. Every stage overridable per
# workspace (stage_role_map) or instance-wide (STAGE_ROLE_MAP env).
DEFAULT_STAGE_ROLE_MAP = {"0": "founder", "1": "founder", "2": "dev", "3": "dev",
                          "4": "dev", "5": "dev", "6": "dev", "7": "dev",
                          "8": "dev", "9": "founder"}


def validate_stage_role_map(mapping) -> dict:
    """Validate a stage→role override map. Keys must be '0'..'9', values
    'founder' | 'dev'. Partial maps are allowed (missing stages fall back to
    the default ladder). Raises ValueError; returns the cleaned mapping."""
    if not isinstance(mapping, dict):
        raise ValueError("stage_role_map must be an object of {stage: role}")
    cleaned: dict = {}
    for stage, role in mapping.items():
        stage = str(stage).strip()
        if stage not in DEFAULT_STAGE_ROLE_MAP:
            raise ValueError(f"stage_role_map stage '{stage}' must be '0'..'9'")
        role = str(role).strip().lower()
        if role not in ("founder", "dev"):
            raise ValueError(f"stage_role_map['{stage}'] must be 'founder' or 'dev', got '{role}'")
        cleaned[stage] = role
    return cleaned


def validate_repo_map(mapping, allow_empty: bool = False) -> dict:
    """Normalize + validate an operator-supplied repo map. Raises ValueError
    with a human-readable reason; returns the cleaned mapping.

    allow_empty=True accepts {} (the neutral fresh-install default — used only
    where an empty map is a valid state, e.g. the setup wizard's default
    comparison). The default stays strict/fail-closed: operator writes may
    never empty a repo set through validation."""
    if isinstance(mapping, dict) and not mapping and allow_empty:
        return {}
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("repo_map must be a non-empty object of {slug: target}")
    cleaned: dict = {}
    for slug, entry in mapping.items():
        slug = str(slug).strip()
        if not slug:
            raise ValueError("repo_map contains an empty project slug")
        if not isinstance(entry, dict):
            raise ValueError(f"repo_map['{slug}'] must be an object")
        repo = str(entry.get("repo") or "").strip()
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ValueError(f"repo_map['{slug}'].repo must be 'owner/name', got '{repo}'")
        allow = entry.get("allow") or []
        if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
            raise ValueError(f"repo_map['{slug}'].allow must be a list of strings")
        cleaned[slug] = {
            "repo": repo,
            "base": str(entry.get("base") or "main").strip() or "main",
            "setup_cmd": (str(entry["setup_cmd"]).strip() or None)
                         if entry.get("setup_cmd") else None,
            "test_cmd": (str(entry["test_cmd"]).strip() or None)
                        if entry.get("test_cmd") else None,
            "allow": allow,
        }
    return cleaned


class Settings(BaseSettings):
    # Sentry — the whole Sentry lane is OFF until both SENTRY_ORG and
    # SENTRY_AUTH_TOKEN are configured (see the sentry_enabled property)
    sentry_org: str = ""
    # EU-region orgs must set SENTRY_API_BASE=https://de.sentry.io/api/0
    sentry_api_base: str = "https://sentry.io/api/0"
    # DEPRECATED/UNUSED: kept only so existing SENTRY_WEB_BASE env vars don't
    # fail settings validation — nothing reads it
    sentry_web_base: str = ""
    # Client secret of the Sentry internal integration (verifies webhook signatures)
    sentry_client_secret: str = ""
    # Auth token of the same internal integration (reads issues, posts comments)
    sentry_auth_token: str = ""

    # GitHub token used for git push + `gh pr create` (fine-grained PAT)
    github_token: str = ""
    # Branch prefix for engine-created branches (<prefix>/feat-…, <prefix>/
    # sentry-…, <prefix>/memory-…). Jobs record their branch at first use, so
    # changing this never strands in-flight work (pre-upgrade rows are
    # backfilled with their historical 'brain/…' branches).
    branch_prefix: str = "ctrlloop"

    @field_validator("branch_prefix")
    @classmethod
    def _validate_branch_prefix(cls, v: str) -> str:
        v = (v or "").strip()
        # one git-valid path segment: no '/', no leading '.', no '.lock' suffix
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", v) or v.endswith(".lock"):
            raise ValueError(
                "branch_prefix must be a single git-valid branch segment "
                "(start alphanumeric; chars A-Za-z0-9._-; no '/', no '.lock' suffix)")
        return v
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
    clickup_list_id: str = ""  # instance-default autofix list; empty = ClickUp off
    clickup_poll_seconds: int = 120  # how often to check awaiting-input tickets
    # ClickUp as an INTAKE channel: tasks named '[fix] …', '[feature] …' or
    # '[sentry <id>] …' in the autofix list are adopted and queued
    clickup_intake_enabled: bool = True
    # Mirror engine state onto a workflow board's custom fields (the Stage
    # board, per-repo PR fields, Decisions, Dashboard link — originally the
    # gumo-speed workflow). Best-effort display only — never drives control
    # flow (ENGINE.md §7). All maps/field names default EMPTY: with no
    # configuration every field-sync helper is inert. A worked example lives
    # in docs/OPERATIONS.md.
    clickup_field_sync_enabled: bool = True
    # engine stage -> Stage dropdown option; the literal value 'build'
    # resolves per-repo via clickup_repo_stage_map
    clickup_stage_field_map: str = "{}"
    # repo (owner/name) -> its build-stage column
    clickup_repo_stage_map: str = "{}"
    # repo (owner/name) -> its PR url field
    clickup_pr_field_map: str = "{}"
    # public dashboard base for deep links (Dashboard field, Slack nudges);
    # empty = no links are emitted
    public_base_url: str = ""
    # artifact mirror -> doc url field (the engine's editable equivalent of
    # doc links), + the folder field pointing at the branch's engine-namespace
    # tree, + the append-only friction log field — empty = skip
    clickup_doc_field_map: str = "{}"
    clickup_folder_field: str = ""
    clickup_friction_field: str = ""
    # P9 launch fields for FLAG_NAME / SUCCESS_METRIC protocol lines — empty = skip
    clickup_flag_field: str = ""
    clickup_metric_field: str = ""
    # role -> the ClickUp people field feature adoption reads that role's DRI
    # from (Epic A2). Engine-generic role names addressed by NAME with a quiet
    # no-op when the workspace lacks them (same posture as Stage/Dashboard/
    # Decisions — see OPERATIONS.md); '{}' disables the reads entirely.
    clickup_dri_field_map: str = '{"founder": "Assigned Founder DRI", "dev": "Assigned Dev DRI"}'

    # ---- Team coordination (Epic A) ----
    # Instance fallback for jobs without a workspace row: auto | on | off.
    # 'auto' = strict once ANY user carries a ClickUp mapping (Epic A1).
    require_attributed_answers: str = "auto"
    # Instance-level stage→role override map (JSON, keys '0'..'9', values
    # founder|dev); '' = the built-in ladder (P0/P1/P9 founder, P2–P8 dev).
    stage_role_map: str = ""
    gate_sla_hours: int = 24            # 0 disables SLA escalation (Epic A5)
    sla_check_interval_seconds: int = 900  # escalation sweep cadence

    # ---- Organizational context (Epic D) ----
    # People profiles fill EMPTY DRI slots at feature intake (exactly-one-match
    # per role, workspace members only — ambiguity fills nothing). Neutral:
    # with an empty people table (every fresh/upgraded install) this is inert.
    # False = profiles feed prompts/display only, never the DRI columns —
    # the opt-out when profiles cover a repo but a team wants DRI-less jobs.
    people_routing_defaults: bool = True
    # FTS memory retrieval: top-k snippets injected into stage prompts.
    # 0 disables the block entirely; values OUTSIDE 1..20 also disable it
    # (never clamped toward permissiveness — same posture as AUTONOMY_AUTO_LEVEL).
    memory_search_top_k: int = 5
    # Slack read ingestion (D3) — FLAG, off by default. Even when enabled it
    # needs the bot token AND a per-workspace channel allowlist.
    slack_ingest_enabled: bool = False
    slack_bot_token: str = ""       # secret; env-only, never in any API response
    slack_api_base: str = "https://slack.com/api"  # test seam (Mixpanel-driver style)
    slack_ingest_interval_seconds: int = 600
    # Pagination bound per channel per pass (pages of 100). A bound-hit pass
    # processes what it fetched but HOLDS the watermark (never advanced past
    # unfetched messages — fail closed); raise this to let a backlogged
    # channel catch up to exhaustion.
    slack_ingest_max_pages: int = 10
    # Reaction NAME that marks a decision thread; the '!decision' message
    # prefix is always recognized when the flag is on.
    slack_decision_emoji: str = "pushpin"

    # ---- Outcome loop (Epic B) ----
    # Default measurement window (days) for features submitted without one.
    metric_window_days_default: int = 14
    # Post-ship watcher: spawn watch jobs on merge + run the watch loop.
    watch_enabled: bool = True
    watch_interval_seconds: int = 3600  # loop cadence; reads throttle to ~daily per job
    # 'flat' verdict band around the baseline, in percent.
    outcome_flat_band_pct: int = 10
    # Write the verdict into product memory via a mechanical draft PR.
    outcome_memory_prs: bool = True
    # Instance-level analytics fallback (per-workspace settings win): provider
    # name ('' = none — the null driver) and its config as a JSON object string
    # ({project_id, service_account, secret, api_base}). Neutral defaults.
    analytics_provider: str = ""
    analytics_config: str = "{}"

    # ---- Graduated autonomy (Epic C, docs/ENGINE.md §15) ----
    # Master switch for the trust ladder: nightly scoring, the autonomy
    # surface, and workspace pins. False = legacy behavior — only the per-job
    # gate_mode='light' path auto-advances; pins are ignored. Env-only (not a
    # RUNTIME_CONTEXT_KEY): flipping it requires a restart.
    autonomy_enabled: bool = True
    autonomy_window_days: int = 30      # rolling stage_runs window the scorer reads
    autonomy_min_runs: int = 5          # cells below this sample stay level 0
    # Computed level required for a cell to auto-advance. FAIL-CLOSED DEFAULT:
    # 0 = computed levels NEVER auto-advance (scores/matrix/pins still work —
    # pins are explicit admin actions). Set 1..3 to opt in (3 = only full
    # trust). Any value outside 1..3 disables the computed-level rule — it is
    # never clamped toward permissiveness.
    autonomy_auto_level: int = 0
    autonomy_recompute_hours: int = 24  # nightly scorer cadence

    # ---- Proactive routines (Epic I, docs/ENGINE.md §17) ----
    # Master flag for the PER-WORKSPACE Epic I routines (standup, memory
    # upkeep, risk scan, proposal scan, planning) ONLY — the builtin
    # sweep/reaper/janitor rows keep firing when it is off ('off' must never
    # mean 'less safe'). Env-only like autonomy_enabled: restart to flip.
    routines_enabled: bool = True
    routine_tick_seconds: int = 60
    routine_tz: str = "UTC"           # schedule evaluation timezone (zoneinfo)
    # Seed defaults ONLY — after the first seed the routine ROW is
    # authoritative (edit via PUT /api/routines/{id}); builtin loops derive
    # their cadence from the live settings instead (schedule='').
    standup_schedule: str = "daily@09:00;days=mon,tue,wed,thu,fri"
    memory_upkeep_schedule: str = "daily@07:00"
    risk_scan_schedule: str = "every:3600"
    proposal_scan_schedule: str = "every:86400"
    planning_schedule: str = "weekly@mon 09:00"
    # Memory upkeep (I3): 0 = inert (opt-in spend — a fresh install queues
    # nothing). Bounded one refresh per repo per cooldown window.
    memory_staleness_threshold: int = 0
    memory_upkeep_cooldown_days: int = 7
    # Risk surfacing (I4): sentry spike alerts off until a threshold is set
    # (absolute 24h event count — no historical snapshot store in v1).
    risk_sentry_spike_events: int = 0
    risk_redo_threshold: int = 3
    # Instance fallback for the per-workspace budget column; 0 = no budget
    # (spend pacing alerts + digest budget section stay inert).
    budget_monthly_usd: float = 0
    # Proposal lane (I5) windows/thresholds.
    proposal_window_days: int = 30
    proposal_friction_min: int = 3
    proposal_sentry_cluster_min: int = 3
    # Inbox notice aging (risk alerts, notes, digests, packs expire visibly)
    # and routine run-history retention (newest N per routine always kept).
    inbox_notice_ttl_days: int = 30
    routine_run_ttl_days: int = 90

    # ---- Audit log (Epic E4) ----
    # Max rows per /api/audit/export page (cursor-paged JSONL for SIEM).
    audit_export_page_size: int = 500
    # 0 = keep forever (the SIEM is the archive); >0 lets the daily janitor
    # prune audit_log rows older than N days.
    audit_retention_days: int = 0

    # ---- Scoped API tokens (Epic E2) ----
    # Default expiry (days) for a token created without an explicit ttl.
    # 0 = no expiry (neutral).
    api_token_default_ttl_days: int = 0

    # ---- Secrets provider & subprocess env allow-list (Epic G2) ----
    # Where sensitive config is read from: 'env' (default — the process
    # environment, what Settings already does), 'file' (SECRETS_DIR/<name>
    # files, Docker/K8s secrets), or 'vault' (scaffold — falls back to env).
    # Unknown values fail closed to 'env'.
    secrets_provider: str = "env"
    secrets_dir: str = ""
    # Extra environment variable NAMES (comma list) to pass THROUGH to Claude/
    # git subprocesses on top of the built-in allow-list. Neutral empty: a run
    # only ever sees plumbing + model-auth + VCS vars + these. The hard-deny
    # secret set (dashboard/OIDC/Sentry/app-key/…) can never be re-added here.
    subprocess_env_allowlist: str = ""

    # ---- GitHub App (Epic G1) — per-repo short-lived installation tokens ----
    # Additive to the PAT (github_token): when both are configured the app mints
    # a fresh installation token per repo per run; a repo the app cannot reach
    # falls back to the PAT. On ANY app error the run falls back to the PAT
    # (fail-open to the working path) — the app is never a hard replacement.
    github_app_id: str = ""
    # SECRET — the app's RSA private key. Supports the '@/path' convention for a
    # mounted PEM file (resolved via secrets.read_secret). Never logged, never
    # placed in any subprocess env.
    github_app_private_key: str = ""
    # Installation-token TTL cache slack: refresh a minted token this many
    # seconds before its stated expiry.
    github_app_token_refresh_slack_seconds: int = 300

    @property
    def github_app_enabled(self) -> bool:
        return bool(self.github_app_id and self.github_app_private_key)

    @property
    def using_max_oauth_token(self) -> bool:
        """Epic G5 policy signal: personal Max OAuth creds are in use with no
        API key set. Anthropic policy forbids routing OTHER users' requests
        through personal Max credentials — a >1-user instance must flip to
        ANTHROPIC_API_KEY (see the startup warning in main.py)."""
        return bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                    and not os.environ.get("ANTHROPIC_API_KEY"))

    # ---- Auth (docs/ENGINE.md §11) ----
    # First-boot admin bootstrap: when the users table is empty, an admin
    # account is created from these. Back-compat: if unset but the legacy
    # DASHBOARD_PASSWORD is set, the admin is created as user "gumo" with that
    # password, so existing deployments keep their credentials.
    ctrlloop_admin_user: str = "admin"
    ctrlloop_admin_password: str = ""
    # Legacy single-credential dashboard auth (pre-CtrlLoop) — bootstrap seed only
    dashboard_password: str = ""
    auth_session_ttl_days: int = 14
    auth_lockout_attempts: int = 5      # consecutive failures before lockout
    auth_lockout_seconds: int = 300
    session_cookie_secure: bool = False  # set true behind TLS-terminating proxies

    # ---- Project context (docs/ENGINE.md §10) ----
    # What the engine works ON. Env vars seed the defaults; operator edits via
    # PUT /api/context persist in the DB and override these at startup.
    # Project slug -> repo config as a JSON string
    repo_map: str = json.dumps(DEFAULT_REPO_MAP)
    # Short product name used in prompt identity lines ("the <name> Engine")
    product_name: str = DEFAULT_PRODUCT_NAME
    # Free-text description of the product/business injected into every prompt
    business_context: str = DEFAULT_BUSINESS_CONTEXT

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
    # project slug hosting product-scope memory; empty = no instance-level
    # product scope (workspaces own their own canonical, ENGINE.md §12)
    memory_canonical_project: str = ""
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
    # Run transcripts (docs/ENGINE.md §13): replayable activity per run, written
    # under data_dir/transcripts and pruned by the same daily janitor. Always on —
    # they are plain files with a hard per-run cap, not a substrate change.
    transcript_ttl_days: int = 30
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

    @property
    def sentry_enabled(self) -> bool:
        """The Sentry lane (webhook intake, sweep, manual triggers, [sentry]
        ticket adoption) requires both the org slug and an auth token."""
        return bool(self.sentry_org and self.sentry_auth_token)

    def stage_runtime_overrides(self, overrides: dict) -> dict:
        """Validate + normalize project-context overrides WITHOUT applying them.
        Raises ValueError on any problem; returns the normalized values. Callers
        that must persist-before-apply (PUT /api/context) stage first, write the
        DB, then apply_staged — so a failed write never leaves live state ahead
        of the persisted state."""
        staged: dict = {}
        for key in RUNTIME_CONTEXT_KEYS:
            if overrides.get(key) is None:
                continue
            value = overrides[key]
            if key == "repo_map":
                mapping = json.loads(value) if isinstance(value, str) else value
                staged[key] = json.dumps(validate_repo_map(mapping))
            else:
                value = str(value).strip()
                # empty business context is a valid choice (no block in prompts);
                # an empty name/canonical slug can only be a mistake — skip it
                if value or key == "business_context":
                    staged[key] = value
        # fail closed: a canonical project outside the map would silently kill
        # product-scope memory for every client-repo run. Checked ONLY when
        # repo_map itself is being staged — workspaces own canonical validation
        # now (WorkspaceService.update), and at startup this method runs BEFORE
        # WorkspaceService.sync_settings rebuilds the merged map, so the live
        # repo_map may legitimately be the empty neutral default. Rejecting a
        # canonical staged alone would atomically drop EVERY persisted override
        # (product_name, business_context) on legacy instances at boot — so a
        # lone canonical is accepted with a logged warning instead. An empty
        # canonical is the valid "no product scope" neutral state, never an error.
        if "repo_map" in staged:
            canonical = staged.get("memory_canonical_project", self.memory_canonical_project)
            if canonical and canonical not in json.loads(staged["repo_map"]):
                raise ValueError(
                    f"canonical project '{canonical}' is not a project slug in the repo map"
                )
        elif "memory_canonical_project" in staged:
            canonical = staged["memory_canonical_project"]
            if canonical and canonical not in json.loads(self.repo_map):
                log.warning(
                    "canonical project '%s' is not in the currently-loaded repo map — "
                    "accepted (workspaces own the merged map; verify it via the "
                    "workspace settings)", canonical)
        return staged

    def apply_staged(self, staged: dict):
        """Apply already-validated overrides to the live settings (cannot fail)."""
        for key, value in staged.items():
            setattr(self, key, value)

    def apply_runtime_overrides(self, overrides: dict) -> dict:
        """Validate and apply in one step — for callers with no persistence to
        order against (startup, tests). Atomic: a ValueError leaves settings
        untouched. Returns the normalized values that were applied."""
        staged = self.stage_runtime_overrides(overrides)
        self.apply_staged(staged)
        return staged

    def project_context(self) -> dict:
        """The effective project context, JSON-decoded (the API/dashboard view)."""
        return {
            "product_name": self.product_name,
            "business_context": self.business_context,
            "repo_map": json.loads(self.repo_map),
            "canonical_project": self.memory_canonical_project,
        }

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

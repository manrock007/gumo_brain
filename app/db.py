import json
import re
import sqlite3
import time
from contextlib import contextmanager

# owner/name + number out of a GitHub PR url (kept escape-simple on purpose)
PR_URL_PARTS_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    issue_id TEXT PRIMARY KEY,       -- Sentry issue id, or 'task-/feat-/mem-<id>' for other kinds
    kind TEXT NOT NULL DEFAULT 'sentry',  -- sentry | task | feature | memory
    project TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    issue_url TEXT DEFAULT '',
    status TEXT NOT NULL,            -- received | queued | running | awaiting_input
                                     -- | pr_opened | no_fix | skipped | error | timeout
    phase INTEGER NOT NULL DEFAULT 1,   -- task/sentry HITL phase (1=analyse, 2=implement)
    stage INTEGER NOT NULL DEFAULT 0,   -- feature pipeline stage (P0..P9)
    stage_attempts INTEGER NOT NULL DEFAULT 0,
    forced INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'webhook',  -- webhook | sweep | manual
    score REAL,
    grade_reasons TEXT,
    request TEXT DEFAULT '',         -- the human-written request (task/feature)
    analysis TEXT,                   -- latest gate payload / phase-1 analysis
    question TEXT DEFAULT '',        -- pending questions extracted from the analysis
    evidence TEXT DEFAULT '',        -- harness-captured gate evidence (diffstat etc.)
    guidance TEXT,                   -- latest human guidance (v1 task flow)
    owner TEXT DEFAULT '',           -- feature owner (ClickUp user id or name); legacy
                                     -- computed alias of the DRI columns (Epic A2)
    founder_dri TEXT DEFAULT '',     -- founder DRI: ClickUp person id (numeric) or username
    dev_dri TEXT DEFAULT '',         -- dev DRI: same encoding
    related_jobs TEXT DEFAULT '',    -- comma-separated sibling pipelines (cross-repo)
    mirror_ok INTEGER NOT NULL DEFAULT 1,  -- ClickUp artifact mirror healthy?
    cu_list_id TEXT DEFAULT '',      -- home list of the ClickUp ticket (for subtasks)
    clickup_task_id TEXT,
    clickup_task_url TEXT,
    comment_marker TEXT DEFAULT '',  -- last ClickUp comment id we processed
    parked_head TEXT DEFAULT '',     -- branch HEAD at gate park (what the human answers against)
    pending_redo_stage INTEGER,      -- set by a redo answer; consumed (reset) by the next run
    resume_session_id TEXT DEFAULT '',  -- STAGE_ASK: session to resume when answered
    resume_stage INTEGER,               -- STAGE_ASK: stage the pending resume belongs to
    resume_attempt INTEGER,             -- STAGE_ASK: attempt the resume continues (no bump)
    resume_head TEXT DEFAULT '',        -- STAGE_ASK: origin head at park (validation)
    resume_answer TEXT DEFAULT '',      -- STAGE_ASK: the human's answer, written by the CAS
    gate_kind TEXT DEFAULT '',          -- '' = normal gate | 'ask' = STAGE_ASK question
    ask_count INTEGER NOT NULL DEFAULT 0,  -- resumes consumed by the current stage attempt
    gate_mode TEXT NOT NULL DEFAULT 'full',  -- full = every stage parks | light = checkpoints
    pr_url TEXT,
    detail TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    run_started_at REAL,             -- set atomically with status='running' (reaper)
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS guidance_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    stage INTEGER,
    action TEXT NOT NULL,            -- proceed | redo | skip
    text TEXT DEFAULT '',
    via TEXT DEFAULT '',             -- dashboard | clickup
    artifact_sha TEXT DEFAULT '',    -- branch HEAD the human answered against
    at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_state (
    job_id TEXT NOT NULL,
    artifact TEXT NOT NULL,          -- e.g. 'P3-design.md'
    subtask_id TEXT DEFAULT '',
    synced_hash TEXT DEFAULT '',     -- semantic hash of the ClickUp READBACK (fixpoint)
    flags TEXT DEFAULT '',           -- '', 'truncated', 'superseded', 'mirror_lost'
    content TEXT NOT NULL DEFAULT '',  -- last-known artifact body (fast-lane chat bundle)
    PRIMARY KEY (job_id, artifact)
);

CREATE TABLE IF NOT EXISTS stage_state (
    job_id TEXT NOT NULL,
    stage INTEGER NOT NULL,
    base_sha TEXT DEFAULT '',        -- branch HEAD before this stage's (latest) run
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (job_id, stage)
);

CREATE TABLE IF NOT EXISTS stage_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    stage INTEGER NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    queued_at REAL,
    started_at REAL,
    ended_at REAL,
    gate_posted_at REAL,
    gate_answered_at REAL,
    gate_action TEXT DEFAULT '',
    cost_usd REAL,
    num_turns INTEGER,
    duration_ms REAL,
    result_status TEXT DEFAULT '',
    session_id TEXT DEFAULT '',      -- the run's CLI session (resumable)
    resumed INTEGER NOT NULL DEFAULT 0,  -- 1 = this run continued a STAGE_ASK session
    transcript TEXT DEFAULT ''       -- run-transcript key under data_dir/transcripts (§13)
);

CREATE TABLE IF NOT EXISTS prs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,        -- one row per PR, however many a packet opens
    repo TEXT NOT NULL DEFAULT '',   -- owner/name parsed from the url
    number INTEGER,
    state TEXT NOT NULL DEFAULT 'draft',  -- draft | ready | in_review | changes_requested | approved | merged | closed
    review_rounds INTEGER NOT NULL DEFAULT 0,  -- @sentry review passes requested (shepherd)
    last_checked REAL,               -- last shepherd poll
    detail TEXT DEFAULT '',          -- latest shepherd note (finding counts, errors)
    approved_head TEXT NOT NULL DEFAULT '',  -- head sha the clean pass approved (re-kick detector)
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,       -- url-safe handle, e.g. 'app'
    name TEXT NOT NULL,              -- display name, e.g. 'App'
    product_name TEXT NOT NULL DEFAULT '',
    workspace_context TEXT NOT NULL DEFAULT '',  -- injected into runs (§10 hierarchy)
    canonical_project TEXT NOT NULL DEFAULT '',  -- project slug hosting product-scope memory
    clickup_list_id TEXT NOT NULL DEFAULT '',    -- empty + disabled -> dashboard-only
    clickup_enabled INTEGER NOT NULL DEFAULT 0,
    slack_webhook_url TEXT NOT NULL DEFAULT '',  -- gate nudges when ClickUp is off (or always)
    gate_mode_default TEXT NOT NULL DEFAULT 'full',
    require_attributed_answers TEXT NOT NULL DEFAULT 'auto',  -- Epic A1: auto | on | off
    stage_role_map TEXT NOT NULL DEFAULT '',     -- Epic A3: JSON overrides; '' = inherit
    gate_sla_hours INTEGER,                      -- Epic A5: NULL = inherit instance default
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_repos (
    workspace_id INTEGER NOT NULL,
    slug TEXT NOT NULL,              -- project slug; UNIQUE across ALL workspaces
    repo TEXT NOT NULL,              -- owner/name
    base TEXT NOT NULL DEFAULT 'main',
    setup_cmd TEXT,
    test_cmd TEXT,
    allow TEXT NOT NULL DEFAULT '[]',  -- JSON list of extra allowed tools
    PRIMARY KEY (workspace_id, slug)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_repos_slug ON workspace_repos(slug);

CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, user_id)
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL,           -- argon2
    role TEXT NOT NULL DEFAULT 'member',  -- admin | member
    clickup_user_id TEXT NOT NULL DEFAULT '',  -- Epic A1: ClickUp person id ↔ CtrlLoop identity
    disabled INTEGER NOT NULL DEFAULT 0,
    must_change_pw INTEGER NOT NULL DEFAULT 0,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until REAL,               -- lockout expiry (consecutive failures)
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,     -- sha256 of the cookie token (never the token itself)
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_seen REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS app_config (
    key TEXT PRIMARY KEY,            -- project-context override key (config.RUNTIME_CONTEXT_KEYS)
    value TEXT NOT NULL,             -- JSON-encoded value
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    stage INTEGER,
    kind TEXT NOT NULL,        -- refused_unattributed | refused_wrong_role | admin_override
                               -- | sla_nudge | sla_second_dri | sla_standup_flag
    ref TEXT NOT NULL,         -- idempotence key: comment id, uuid, or 'run<stage_runs.id>-step<k>'
    detail TEXT DEFAULT '',
    actor TEXT DEFAULT '',     -- acting/refused identity, e.g. 'clickup:jane#123', 'dashboard:manish'
    at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gate_events_dedupe ON gate_events(job_id, kind, ref);

-- Append-only audit of admin/config mutations that grant or move authority
-- (user↔ClickUp identity links, workspace security config). INSERT-only —
-- the minimal substrate the BUILD-PLAN invariant ("every new mutation is
-- auditable") requires until Epic E4's full audit_log folds/exports it.
CREATE TABLE IF NOT EXISTS admin_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- 'clickup_link' | 'workspace_config' | 'workspace_create'
    target TEXT NOT NULL DEFAULT '', -- mutated entity: username / workspace id
    detail TEXT DEFAULT '',          -- what changed (secrets redacted at the call site)
    actor TEXT DEFAULT '',           -- acting principal, e.g. 'dashboard:<username>'
    at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,            -- the watch job (INSERT-only, one row per read)
    metric TEXT NOT NULL DEFAULT '',
    metric_event TEXT NOT NULL DEFAULT '',
    observed REAL,                   -- window-to-date aggregate at read time
    window_day INTEGER,              -- 1..N day index inside the watch window
    window_start REAL,               -- the watch_started_at this reading belongs to
                                     -- (a /redo re-arms a NEW window; verdicts and
                                     -- gate tables must never mix two windows)
    detail TEXT NOT NULL DEFAULT '', -- provider note (series summary)
    at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,     -- the watch job (watch-<feature id>)
    feature_id TEXT NOT NULL DEFAULT '',
    workspace_id INTEGER,
    metric TEXT NOT NULL DEFAULT '',
    metric_event TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    observed REAL,
    baseline REAL,                   -- pre-merge same-length window aggregate (when queryable)
    window_days INTEGER,
    verdict TEXT NOT NULL DEFAULT '',      -- moved | flat | regressed | unmeasured
    verdict_inputs TEXT NOT NULL DEFAULT '{}',  -- JSON: formula inputs (transparent, auditable)
    learning TEXT NOT NULL DEFAULT '',     -- filled by the human's /proceed answer
    decided_by TEXT NOT NULL DEFAULT '',   -- via string, e.g. 'dashboard:manish' / 'clickup:jane'
    decided_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS autonomy_scores (
    workspace_id INTEGER NOT NULL,
    project TEXT NOT NULL,             -- repo slug (globally unique across workspaces)
    stage INTEGER NOT NULL,            -- 0..8 (P9 never auto-advances — no cell)
    level INTEGER NOT NULL DEFAULT 0,  -- 0 = always gate .. 3 = full auto-advance
    score REAL NOT NULL DEFAULT 0,     -- composite 0..1 the level was derived from
    inputs TEXT NOT NULL DEFAULT '{}', -- JSON: exact numbers the formula saw (transparency)
    sample_runs INTEGER NOT NULL DEFAULT 0,
    clawback_at REAL,                  -- set by clawback; runs before this never count again
    computed_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, project, stage)
);

CREATE TABLE IF NOT EXISTS autonomy_pins (
    workspace_id INTEGER NOT NULL,
    stage INTEGER NOT NULL,
    pin TEXT NOT NULL,                 -- 'always_gate' | 'always_auto'
    set_by TEXT NOT NULL DEFAULT '',   -- 'dashboard:<username>'
    set_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, stage)
);

CREATE TABLE IF NOT EXISTS autonomy_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER,
    project TEXT DEFAULT '',
    stage INTEGER,
    job_id TEXT DEFAULT '',
    kind TEXT NOT NULL,                -- 'auto_advance' | 'pin_set' | 'pin_clear' | 'clawback' | 'level_change'
    detail TEXT DEFAULT '',            -- e.g. 'P6 auto-advanced — level 3, 14 clean runs'
    actor TEXT DEFAULT '',             -- 'engine' | 'dashboard:<username>'
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_autonomy_events_ws_at ON autonomy_events(workspace_id, at);

-- Epic D2: cross-ticket decision registry. Auto-registered from substantive
-- gate answers (ref='g<guidance id>' — idempotent under replay), manual adds
-- from the dashboard, Slack candidates (D3 — status='candidate', quarantined:
-- never indexed, never in prompts, never in the default registry view until
-- a human confirms). origin_author preserves the ORIGINAL author when a
-- confirmation stamps decided_by with the ratifying human (auditability).
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'gate',    -- gate | manual | slack
    status TEXT NOT NULL DEFAULT 'active',  -- active | superseded | candidate | dismissed
    scope TEXT NOT NULL DEFAULT 'job',      -- job | repo | product | org
    job_id TEXT NOT NULL DEFAULT '',
    workspace_id INTEGER,
    project TEXT NOT NULL DEFAULT '',
    stage INTEGER,
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,                     -- decision + rationale (capped 4000 at write)
    decided_by TEXT NOT NULL DEFAULT '',    -- via string: 'dashboard:<u>' | 'clickup:<u>' | 'slack:<u>'
    origin_author TEXT NOT NULL DEFAULT '', -- original author when decided_by is later overwritten
    links TEXT NOT NULL DEFAULT '[]',       -- JSON list of URLs
    ref TEXT NOT NULL DEFAULT '',           -- idempotence key ('' allowed for manual adds)
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_ref ON decisions(source, ref) WHERE ref != '';

-- Epic D3: per-channel Slack read watermarks. Deliberately NOT app_config
-- (that is the context-override KV). last_ts is Slack's ts string
-- (lexicographic-safe); rows are created — initialized to NOW — when a
-- channel is first allowlisted, so ingestion is forward-only by construction
-- (no historical candidate flood).
CREATE TABLE IF NOT EXISTS slack_cursors (
    channel TEXT PRIMARY KEY,
    last_ts TEXT NOT NULL DEFAULT '0',
    updated_at REAL NOT NULL
);

-- Epic D1: people profile layer OVER users (1:1; never a parallel identity).
-- Feeds intake-time DRI defaults + the prompt ownership block; gate
-- enforcement still keys exclusively on the jobs.founder_dri/dev_dri columns.
CREATE TABLE IF NOT EXISTS people (
    user_id INTEGER PRIMARY KEY,          -- users(id)
    person_role TEXT NOT NULL DEFAULT '', -- '' | founder | product | dev | design
    areas TEXT NOT NULL DEFAULT '[]',     -- JSON list of {"kind": workspace|repo|area, "value": str}
    authority TEXT NOT NULL DEFAULT '[]', -- JSON list of decision-authority tags
    notes TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);

-- Epic I0: durable inbox notices — every proactive-routine output lands here
-- (risk alerts, proposal briefs, standup digests, planning packs, routine
-- notes), never a silent side effect. UNIQUE(kind, dedupe_key) is BOTH the
-- re-scan idempotence guard and the DISMISSAL MEMORY: rows are never deleted,
-- so a dismissed key blocks re-insert forever; only a candidate whose
-- contributing content changed (folded into the key) can surface again.
CREATE TABLE IF NOT EXISTS inbox_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER,            -- NULL = instance-wide (admin-only visibility)
    kind TEXT NOT NULL,              -- 'risk_alert' | 'proposal' | 'standup_digest'
                                     -- | 'planning_pack' | 'routine_note'
    source TEXT NOT NULL DEFAULT '', -- emitting routine kind, e.g. 'risk_scan'
    dedupe_key TEXT NOT NULL,        -- idempotence + dismissal memory (see above)
    source_sig TEXT NOT NULL DEFAULT '',  -- coarse source signature for the
                                     -- proposal recency guard (project:stage etc.)
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',   -- markdown; fed to prompts ONLY via adoption,
                                     -- where it takes the untrusted-fragment
                                     -- posture of a ClickUp description
    refs TEXT NOT NULL DEFAULT '{}', -- JSON: {job_id, project, pr_url, ...}
    status TEXT NOT NULL DEFAULT 'open',  -- open | dismissed | adopted | expired
    status_by TEXT NOT NULL DEFAULT '',   -- 'dashboard:<user>' / 'engine'
    status_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_items_dedupe ON inbox_items(kind, dedupe_key);

-- Epic I5: friction becomes engine data, not just a ClickUp mirror. Rows come
-- from run FRICTION: protocol lines AND human redos — written regardless of
-- clickup_field_sync_enabled (the row is the record; the mirror stays
-- best-effort visibility, the exact Epic B posture).
CREATE TABLE IF NOT EXISTS frictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    workspace_id INTEGER,
    project TEXT NOT NULL DEFAULT '',
    stage INTEGER,
    source TEXT NOT NULL,            -- 'run' | 'redo'
    text TEXT NOT NULL,
    at REAL NOT NULL
);

-- Epic I1: the routine engine. One row per (kind, scope); builtin instance
-- rows (workspace_id NULL) generalize the hardcoded loops. schedule='' on a
-- builtin row means "derive from live settings at each tick" so env contracts
-- (SWEEP_INTERVAL_HOURS, …) keep working; an operator-edited non-empty
-- schedule wins. last_run_at doubles as the CAS claim column.
-- NOTE (Epic F1): the COALESCE expression index needs Postgres expression-
-- index syntax review when the Alembic baseline is generated.
CREATE TABLE IF NOT EXISTS routines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER,             -- NULL = instance-scoped (builtin loops)
    kind TEXT NOT NULL,               -- registry key
    name TEXT NOT NULL DEFAULT '',
    schedule TEXT NOT NULL,           -- 'every:<seconds>' | 'daily@HH:MM[;days=…]'
                                      -- | 'weekly@<day> HH:MM' | '' (builtin: derive)
    config TEXT NOT NULL DEFAULT '{}',-- JSON per-kind knobs (override settings)
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at REAL,                 -- CAS claim column
    last_status TEXT NOT NULL DEFAULT '',   -- ok | error | quiet | skipped
    last_result TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_routines_scope
    ON routines(kind, COALESCE(workspace_id, -1));

CREATE TABLE IF NOT EXISTS routine_runs (    -- INSERT-only run history
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    workspace_id INTEGER,
    started_at REAL NOT NULL,
    ended_at REAL,
    status TEXT NOT NULL DEFAULT '',  -- ok | error | quiet | skipped
    detail TEXT NOT NULL DEFAULT '',
    items_emitted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS gate_chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    stage INTEGER NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    role TEXT NOT NULL,              -- human | engine
    text TEXT NOT NULL,
    cost_usd REAL,                   -- engine turns: CLI envelope telemetry
    num_turns INTEGER,
    duration_ms REAL,
    session_id TEXT DEFAULT '',
    degraded INTEGER NOT NULL DEFAULT 0,  -- answered from documents only (no session)
    lane TEXT NOT NULL DEFAULT '',   -- '' = tool run | 'fast' = bundle-primed API call
    at REAL NOT NULL
);
"""

MIGRATIONS = {  # table -> columns added after that table first shipped (in-place upgrade)
    "jobs": {
        "kind": "TEXT NOT NULL DEFAULT 'sentry'",
        "request": "TEXT DEFAULT ''",
        "question": "TEXT DEFAULT ''",
        "stage": "INTEGER NOT NULL DEFAULT 0",
        "stage_attempts": "INTEGER NOT NULL DEFAULT 0",
        "evidence": "TEXT DEFAULT ''",
        "owner": "TEXT DEFAULT ''",
        "related_jobs": "TEXT DEFAULT ''",
        "mirror_ok": "INTEGER NOT NULL DEFAULT 1",
        "cu_list_id": "TEXT DEFAULT ''",
        "run_started_at": "REAL",
        "parked_head": "TEXT DEFAULT ''",
        "pending_redo_stage": "INTEGER",
        "resume_session_id": "TEXT DEFAULT ''",
        "resume_stage": "INTEGER",
        "resume_attempt": "INTEGER",
        "resume_head": "TEXT DEFAULT ''",
        "resume_answer": "TEXT DEFAULT ''",
        "gate_kind": "TEXT DEFAULT ''",  # '' = normal | 'ask' = STAGE_ASK | 'steer' = mid-run interrupt
        "ask_count": "INTEGER NOT NULL DEFAULT 0",
        "gate_mode": "TEXT NOT NULL DEFAULT 'full'",
        "steer_note": "TEXT DEFAULT ''",  # human's live steer, moved into resume_answer on interrupt
        "auto_retries": "INTEGER NOT NULL DEFAULT 0",  # transient-error retries spent (max 1; human redo resets)
        "workspace_id": "INTEGER",  # owning workspace (Phase 2); NULL rows adopted by migration
        "branch": "TEXT NOT NULL DEFAULT ''",  # git branch this job's work lives on (stamped at first use)
        "founder_dri": "TEXT DEFAULT ''",  # Epic A2: ClickUp person id (numeric) or username
        "dev_dri": "TEXT DEFAULT ''",      # Epic A2: same encoding; legacy `owner` = computed alias
        "success_metric": "TEXT DEFAULT ''",   # Epic B1: metric name/goal captured at intake
        "metric_target": "TEXT DEFAULT ''",    # Epic B1: target value as text (numeric when parseable)
        "metric_window_days": "INTEGER",       # Epic B1: NULL = settings default at watch spawn
        "metric_event": "TEXT DEFAULT ''",     # Epic B2: analytics event, harvested from P9 METRIC_EVENT:
        "watch_started_at": "REAL",            # Epic B4: watch jobs only
        "watch_deadline": "REAL",              # Epic B4: persisted so restarts never re-derive the window
        # Epic I5: sentry-cluster substrate — the issue culprit, single-lined
        # and capped at write (worker._process_sentry). Pre-upgrade sentry rows
        # keep culprit='' — clusters only accumulate from upgrade forward.
        "culprit": "TEXT DEFAULT ''",
    },
    "users": {
        "clickup_user_id": "TEXT NOT NULL DEFAULT ''",  # Epic A1: ClickUp id ↔ CtrlLoop identity
    },
    "workspaces": {
        "require_attributed_answers": "TEXT NOT NULL DEFAULT 'auto'",  # Epic A1: auto | on | off
        "stage_role_map": "TEXT NOT NULL DEFAULT ''",                  # Epic A3: JSON; '' = inherit
        "gate_sla_hours": "INTEGER",                                   # Epic A5: NULL = inherit
        "analytics_provider": "TEXT NOT NULL DEFAULT ''",  # Epic B3: '' = none | 'mixpanel'
        # SECRET AT REST — never returned by the API (workspaces.public redacts it)
        "analytics_config": "TEXT NOT NULL DEFAULT '{}'",
        # Epic D3: JSON list of Slack channel ids to ingest (FLAG'd feature);
        # '' = ingestion off for this workspace. Channel ids are not secrets.
        "slack_channels": "TEXT NOT NULL DEFAULT ''",
        # Epic I4 (and the substrate Epic G4 extends into the warn/block
        # ladder): per-workspace monthly budget in USD. NULL = inherit the
        # instance BUDGET_MONTHLY_USD; 0 = no budget (spend alerts inert).
        "budget_monthly_usd": "REAL",
    },
    "stage_runs": {
        "session_id": "TEXT DEFAULT ''",
        "resumed": "INTEGER NOT NULL DEFAULT 0",
        "transcript": "TEXT DEFAULT ''",
    },
    "gate_chat": {
        "cost_usd": "REAL",
        "num_turns": "INTEGER",
        "duration_ms": "REAL",
        "session_id": "TEXT DEFAULT ''",
        "degraded": "INTEGER NOT NULL DEFAULT 0",
        "lane": "TEXT NOT NULL DEFAULT ''",
        "author": "TEXT NOT NULL DEFAULT ''",  # human turns: acting username
    },
    "artifact_state": {
        "content": "TEXT NOT NULL DEFAULT ''",
    },
    "prs": {
        "approved_head": "TEXT NOT NULL DEFAULT ''",
    },
}

# Artifact bodies cached for the fast-lane chat bundle are capped — they are
# markdown documents, not blobs; anything longer is truncated with a banner.
ARTIFACT_CONTENT_MAX = 60000

# Epic D4: FTS index body cap and the caps the registry enforces at write.
FTS_BODY_MAX = 20000
DECISION_TEXT_MAX = 4000
DECISION_SCOPES = ("job", "repo", "product", "org")
DECISION_STATUSES = ("active", "superseded", "candidate", "dismissed")

# FTS terms are sanitized HARD before they reach a MATCH expression: FTS5
# syntax injection is a crash vector, not a security one, but a crash in
# prompt assembly would park the job.
_FTS_TERM_RE = re.compile(r"[^0-9A-Za-z_]+")


class JobStore:
    def __init__(self, path: str):
        self._path = path
        with self._conn() as c:
            c.executescript(SCHEMA)
            for table, columns in MIGRATIONS.items():
                cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
                for col, ddl in columns.items():
                    if col not in cols:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                        if table == "jobs" and col == "branch":
                            # one-time backfill on the upgrade boot (idempotent
                            # by construction — it only runs when the column is
                            # first added): every pre-upgrade job keeps the
                            # EXACT branch it already pushed under the
                            # historical 'brain/' prefix. Feature job ids
                            # already carry 'feat-', so 'brain/feat-feat-<id>'
                            # is deliberate — it matches what the engine built.
                            c.execute("""UPDATE jobs SET branch = CASE kind
                                WHEN 'feature' THEN 'brain/feat-' || issue_id
                                WHEN 'memory'  THEN 'brain/memory-' || project
                                WHEN 'sentry'  THEN 'brain/sentry-' || issue_id
                                ELSE 'brain/' || issue_id END
                                WHERE branch = ''""")
            # after the migrations loop so upgraded DBs already carry the column:
            # one ClickUp identity maps to at most one CtrlLoop user (Epic A1).
            # Partial (non-empty only) — pre-existing rows are all '', so the
            # index creation is always safe.
            c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_users_clickup_id
                         ON users(clickup_user_id) WHERE clickup_user_id != ''""")
            # Epic D4: the memory-retrieval index lives beside the data it
            # mirrors. SQLite built without FTS5 (exotic) degrades to ABSENT
            # retrieval — fail-open ONLY for a read enhancement: no control
            # flow ever depends on the index; the prompt block just never
            # renders. Candidates (D3) are never indexed by construction.
            try:
                c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(
                    kind UNINDEXED, key UNINDEXED, project UNINDEXED,
                    workspace_id UNINDEXED, scope UNINDEXED, job_id UNINDEXED,
                    path UNINDEXED, title, body, tokenize='porter unicode61')""")
                self.fts_enabled = True
            except sqlite3.OperationalError:
                self.fts_enabled = False

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------- jobs ----------

    def get(self, issue_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE issue_id = ?", (issue_id,)).fetchone()
            return dict(row) if row else None

    def job_count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]

    def runs_today(self) -> int:
        """Claude invocations started since midnight (statuses past grading)."""
        midnight = time.time() - (time.time() % 86400)
        with self._conn() as c:
            row = c.execute(
                """SELECT COUNT(*) AS n FROM jobs WHERE updated_at >= ?
                   AND status IN ('running', 'awaiting_input', 'pr_opened', 'no_fix', 'error', 'timeout')""",
                (midnight,),
            ).fetchone()
            return row["n"]

    def insert(self, issue_id: str, source: str, forced: bool = False,
               title: str = "", project: str = "", kind: str = "sentry"):
        """Insert or re-open a job. Re-intake resets the task/sentry HITL state;
        feature pipeline state (stage, child tables) is reset explicitly by
        Worker.intake_feature, never here."""
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO jobs (issue_id, kind, project, title, status, source, forced, attempts, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'received', ?, ?, 1, ?, ?)
                   ON CONFLICT(issue_id) DO UPDATE SET
                     status = 'received',
                     source = excluded.source,
                     forced = MAX(jobs.forced, excluded.forced),
                     phase = CASE WHEN excluded.kind = 'feature' THEN jobs.phase ELSE 1 END,
                     analysis = CASE WHEN excluded.kind = 'feature' THEN jobs.analysis ELSE NULL END,
                     question = CASE WHEN excluded.kind = 'feature' THEN jobs.question ELSE '' END,
                     guidance = CASE WHEN excluded.kind = 'feature' THEN jobs.guidance ELSE NULL END,
                     attempts = jobs.attempts + 1,
                     updated_at = excluded.updated_at""",
                (issue_id, kind, project, title, source, int(forced), now, now),
            )

    def set_fields(self, issue_id: str, **fields):
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(
                f"UPDATE jobs SET {cols}, updated_at = ? WHERE issue_id = ?",
                (*fields.values(), time.time(), issue_id),
            )

    def set_status(self, issue_id: str, status: str, pr_url: str | None = None, detail: str | None = None):
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET status = ?, pr_url = COALESCE(?, pr_url), detail = COALESCE(?, detail), updated_at = ? WHERE issue_id = ?",
                (status, pr_url, detail, time.time(), issue_id),
            )

    def cas_status(self, issue_id: str, from_statuses: list[str], to_status: str,
                   expected_stage: int | None = None, **fields) -> bool:
        """Atomic compare-and-set transition — the single-writer gate guard.
        Returns True iff this caller won the transition. `fields` may include a
        new `stage`; `expected_stage` guards the CURRENT one."""
        marks = ",".join("?" for _ in from_statuses)
        cols = "".join(f", {k} = ?" for k in fields)
        where_stage = " AND stage = ?" if expected_stage is not None else ""
        params = [to_status, *fields.values(), time.time(), issue_id, *from_statuses]
        if expected_stage is not None:
            params.append(expected_stage)
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE jobs SET status = ?{cols}, updated_at = ? "
                f"WHERE issue_id = ? AND status IN ({marks}){where_stage}",
                params,
            )
            return cur.rowcount == 1

    def by_status(self, statuses: list[str]) -> list[dict]:
        marks = ",".join("?" for _ in statuses)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY updated_at DESC",
                statuses,
            ).fetchall()
            return [dict(r) for r in rows]

    def recent(self, limit: int = 200) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def known_issue_ids(self) -> set[str]:
        with self._conn() as c:
            return {r["issue_id"] for r in c.execute("SELECT issue_id FROM jobs").fetchall()}

    def job_for_clickup_task(self, task_id: str) -> dict | None:
        """The job a ClickUp ticket belongs to — the intake scan's dedupe:
        engine-created tickets and already-adopted ones both have a row."""
        if not task_id:
            return None
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE clickup_task_id = ? LIMIT 1",
                            (str(task_id),)).fetchone()
            return dict(row) if row else None

    def requeueable(self) -> list[dict]:
        """Jobs that must be re-enqueued on startup — SQLite is the queue of record."""
        return self.by_status(["received", "queued"])

    def stale_running(self, older_than_seconds: float) -> list[dict]:
        cutoff = time.time() - older_than_seconds
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM jobs WHERE status = 'running' AND COALESCE(run_started_at, 0) < ? AND COALESCE(run_started_at, 0) > 0",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- workspaces (docs/ENGINE.md §12) ----------

    def workspace_list(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM workspaces ORDER BY name").fetchall()
            return [dict(r) for r in rows]

    def workspace_get(self, workspace_id: int) -> dict | None:
        """By numeric id ONLY — slugs may be all-numeric, so id-vs-slug must
        never be guessed from the value (sentry finding 1595569)."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM workspaces WHERE id = ?",
                            (int(workspace_id),)).fetchone()
            return dict(row) if row else None

    def workspace_get_by_slug(self, slug: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM workspaces WHERE slug = ?",
                            (str(slug),)).fetchone()
            return dict(row) if row else None

    def workspace_create(self, slug: str, name: str, **fields) -> dict:
        now = time.time()
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._conn() as c:
            c.execute(
                f"INSERT INTO workspaces (slug, name{', ' + cols if cols else ''}, created_at, updated_at) "
                f"VALUES (?, ?{', ' + marks if marks else ''}, ?, ?)",
                (slug, name, *fields.values(), now, now),
            )
        return self.workspace_get_by_slug(slug)

    def workspace_set(self, workspace_id: int, **fields):
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE workspaces SET {cols}, updated_at = ? WHERE id = ?",
                      (*fields.values(), time.time(), workspace_id))

    def workspace_repos_for(self, workspace_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM workspace_repos WHERE workspace_id = ? ORDER BY slug",
                             (workspace_id,)).fetchall()
            return [dict(r) for r in rows]

    def workspace_repos_replace(self, workspace_id: int, rows: list[dict]):
        """Replace a workspace's repo set in one transaction. The UNIQUE index
        on slug enforces global slug uniqueness — violations raise IntegrityError."""
        with self._conn() as c:
            c.execute("DELETE FROM workspace_repos WHERE workspace_id = ?", (workspace_id,))
            for r in rows:
                c.execute(
                    "INSERT INTO workspace_repos (workspace_id, slug, repo, base, setup_cmd, test_cmd, allow) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (workspace_id, r["slug"], r["repo"], r.get("base") or "main",
                     r.get("setup_cmd"), r.get("test_cmd"), json.dumps(r.get("allow") or [])),
                )

    def repo_rows_all(self) -> list[dict]:
        """Every repo row across workspaces (slug is globally unique)."""
        with self._conn() as c:
            rows = c.execute("SELECT * FROM workspace_repos ORDER BY slug").fetchall()
            return [dict(r) for r in rows]

    def workspace_for_slug(self, project_slug: str) -> dict | None:
        """The workspace owning a project slug — webhook routing + canonical
        memory resolution."""
        with self._conn() as c:
            row = c.execute(
                """SELECT w.* FROM workspace_repos r JOIN workspaces w ON w.id = r.workspace_id
                   WHERE r.slug = ?""", (project_slug,)).fetchone()
            return dict(row) if row else None

    def workspace_members_get(self, workspace_id: int) -> list[str]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT u.username FROM workspace_members m JOIN users u ON u.id = m.user_id
                   WHERE m.workspace_id = ? ORDER BY u.username""", (workspace_id,)).fetchall()
            return [r["username"] for r in rows]

    def workspace_member_set(self, workspace_id: int, user_id: int, member: bool):
        with self._conn() as c:
            if member:
                c.execute("INSERT OR IGNORE INTO workspace_members (workspace_id, user_id) VALUES (?, ?)",
                          (workspace_id, user_id))
            else:
                c.execute("DELETE FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
                          (workspace_id, user_id))

    def workspace_ids_for_user(self, user_id: int) -> set[int]:
        with self._conn() as c:
            rows = c.execute("SELECT workspace_id FROM workspace_members WHERE user_id = ?",
                             (user_id,)).fetchall()
            return {r["workspace_id"] for r in rows}

    def jobs_adopt_workspace(self, workspace_id: int):
        """Migration helper: attach every unowned job to the given workspace."""
        with self._conn() as c:
            c.execute("UPDATE jobs SET workspace_id = ? WHERE workspace_id IS NULL",
                      (workspace_id,))

    def migrate_default_workspace(self, slug: str, name: str, fields: dict,
                                  repos: list[dict], user_ids: list[int]) -> dict:
        """The upgrade migration in ONE transaction: workspace + repos + job
        adoption + memberships commit together or not at all — a crash can
        never leave a half-built default that the existence guard would then
        treat as migrated (sentry finding 1595858)."""
        now = time.time()
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._conn() as c:
            cur = c.execute(
                f"INSERT INTO workspaces (slug, name{', ' + cols if cols else ''}, created_at, updated_at) "
                f"VALUES (?, ?{', ' + marks if marks else ''}, ?, ?)",
                (slug, name, *fields.values(), now, now),
            )
            ws_id = cur.lastrowid
            for r in repos:
                c.execute(
                    "INSERT INTO workspace_repos (workspace_id, slug, repo, base, setup_cmd, test_cmd, allow) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ws_id, r["slug"], r["repo"], r.get("base") or "main",
                     r.get("setup_cmd"), r.get("test_cmd"), json.dumps(r.get("allow") or [])),
                )
            c.execute("UPDATE jobs SET workspace_id = ? WHERE workspace_id IS NULL", (ws_id,))
            for uid in user_ids:
                c.execute("INSERT OR IGNORE INTO workspace_members (workspace_id, user_id) VALUES (?, ?)",
                          (ws_id, uid))
        return self.workspace_get(ws_id)

    # ---------- users & auth sessions (docs/ENGINE.md §11) ----------

    def user_get(self, username: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE username = ?",
                            ((username or "").strip(),)).fetchone()
            return dict(row) if row else None

    def user_count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    def user_list(self) -> list[dict]:
        """All users WITHOUT pw_hash — safe for the admin UI."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, username, role, clickup_user_id, disabled, must_change_pw, "
                "created_at, updated_at FROM users ORDER BY username").fetchall()
            return [dict(r) for r in rows]

    def user_for_clickup_id(self, cu_id: str) -> dict | None:
        """The ENABLED user a ClickUp identity maps to. Fails closed on
        ambiguity: 0 or >1 matches both return None (the unique partial index
        prevents duplicates, but a hand-edited DB must not silently pick one)."""
        cu_id = str(cu_id or "").strip()
        if not cu_id:
            return None
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM users WHERE clickup_user_id = ? AND disabled = 0 LIMIT 2",
                (cu_id,)).fetchall()
            return dict(rows[0]) if len(rows) == 1 else None

    def any_clickup_mapping(self) -> bool:
        """Does ANY enabled user carry a ClickUp mapping? Drives the 'auto'
        strictness of require_attributed_answers (Epic A1)."""
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM users WHERE clickup_user_id != '' AND disabled = 0 LIMIT 1"
            ).fetchone() is not None

    def user_for_dri(self, value: str) -> dict | None:
        """Resolve a DRI value (ClickUp person id or CtrlLoop username) to a
        user row. Numeric values are ClickUp ids first; a miss falls back to a
        username lookup so a digits-only username stays reachable."""
        value = str(value or "").strip()
        if not value:
            return None
        if value.isdigit():
            user = self.user_for_clickup_id(value)
            if user is not None:
                return user
        return self.user_get(value)

    # ---------- people profiles (Epic D1 — a layer over users, never identity) ----------

    def person_get(self, user_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM people WHERE user_id = ?",
                            (int(user_id),)).fetchone()
            return dict(row) if row else None

    def person_set(self, user_id: int, **fields):
        """UPSERT the profile row in a single statement. Callers validate via
        people.validate_profile FIRST — a bad profile must change nothing."""
        allowed = {k: v for k, v in fields.items()
                   if k in ("person_role", "areas", "authority", "notes")}
        if not allowed:
            return
        now = time.time()
        cols = ", ".join(allowed)
        marks = ", ".join("?" for _ in allowed)
        sets = ", ".join(f"{k} = excluded.{k}" for k in allowed)
        with self._conn() as c:
            c.execute(
                f"INSERT INTO people (user_id, {cols}, updated_at) "
                f"VALUES (?, {marks}, ?) "
                f"ON CONFLICT(user_id) DO UPDATE SET {sets}, "
                f"updated_at = excluded.updated_at",
                (int(user_id), *allowed.values(), now),
            )

    def people_all(self) -> list[dict]:
        """Every user with their profile (empty defaults when no row) —
        excludes pw_hash, mirrors user_list's safe shape."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT u.id, u.username, u.role, u.clickup_user_id, u.disabled,
                          COALESCE(p.person_role, '') AS person_role,
                          COALESCE(p.areas, '[]') AS areas,
                          COALESCE(p.authority, '[]') AS authority,
                          COALESCE(p.notes, '') AS notes
                   FROM users u LEFT JOIN people p ON p.user_id = u.id
                   ORDER BY u.username""").fetchall()
            return [dict(r) for r in rows]

    def user_create(self, username: str, pw_hash: str, role: str = "member",
                    must_change_pw: bool = True) -> dict:
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO users (username, pw_hash, role, must_change_pw, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (username.strip(), pw_hash, role, int(must_change_pw), now, now),
            )
        return self.user_get(username)

    def user_set(self, username: str, **fields):
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE users SET {cols}, updated_at = ? WHERE username = ?",
                      (*fields.values(), time.time(), username))

    def user_record_failure(self, username: str, max_attempts: int, lockout_seconds: int):
        """Bump the consecutive-failure counter; lock the account when it hits
        the cap. Counter resets on success (auth.verify_login)."""
        with self._conn() as c:
            # every SET expression sees the OLD row, so both CASEs key off the
            # same pre-increment counter: hitting the cap locks and resets it
            c.execute(
                """UPDATE users SET
                     locked_until = CASE WHEN failed_attempts + 1 >= ? THEN ? ELSE locked_until END,
                     failed_attempts = CASE WHEN failed_attempts + 1 >= ? THEN 0 ELSE failed_attempts + 1 END,
                     updated_at = ?
                   WHERE username = ?""",
                (max_attempts, time.time() + lockout_seconds, max_attempts,
                 time.time(), username),
            )

    # -- cookie sessions (token stored hashed; sha256 is fine for 256-bit random tokens)

    def auth_session_create(self, token_hash: str, user_id: int, ttl_seconds: float):
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?)",
                (token_hash, user_id, now, now + ttl_seconds, now),
            )

    def auth_session_user(self, token_hash: str) -> dict | None:
        """The (enabled) user behind a live session; touches last_seen."""
        now = time.time()
        with self._conn() as c:
            row = c.execute(
                """SELECT u.* FROM auth_sessions s JOIN users u ON u.id = s.user_id
                   WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0""",
                (token_hash, now),
            ).fetchone()
            if row is None:
                return None
            c.execute("UPDATE auth_sessions SET last_seen = ? WHERE token_hash = ?",
                      (now, token_hash))
            return dict(row)

    def auth_session_delete(self, token_hash: str):
        with self._conn() as c:
            c.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))

    def auth_sessions_revoke_user(self, user_id: int):
        """Password change / disable kills every live session for the user."""
        with self._conn() as c:
            c.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))

    def auth_sessions_prune(self):
        with self._conn() as c:
            c.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (time.time(),))

    # ---------- app config (project-context overrides, key-value) ----------

    def config_all(self) -> dict:
        """Every persisted override, JSON-decoded: {key: value}."""
        with self._conn() as c:
            rows = c.execute("SELECT key, value FROM app_config").fetchall()
        out = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except (ValueError, TypeError):
                continue  # a corrupt row must not take the app down
        return out

    def config_set(self, key: str, value):
        self.config_set_many({key: value})

    def config_set_many(self, values: dict):
        """Upsert several overrides in ONE transaction — a partial override set
        can be self-inconsistent (e.g. a repo map without its canonical slug)
        and would be rejected wholesale at the next startup."""
        now = time.time()
        with self._conn() as c:
            for key, value in values.items():
                c.execute(
                    """INSERT INTO app_config (key, value, updated_at) VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                         value = excluded.value, updated_at = excluded.updated_at""",
                    (key, json.dumps(value), now),
                )

    def config_clear(self, keys: list[str] | None = None):
        """Remove overrides (all of them when keys is None) — revert to defaults."""
        with self._conn() as c:
            if keys is None:
                c.execute("DELETE FROM app_config")
            else:
                marks = ",".join("?" for _ in keys)
                c.execute(f"DELETE FROM app_config WHERE key IN ({marks})", keys)

    # ---------- guidance log (INSERT-only) ----------

    def guidance_add(self, job_id: str, stage: int | None, action: str, text: str,
                     via: str, artifact_sha: str = "") -> int:
        """INSERT-only; returns the new row id (Epic D2: the decision
        registry's auto-registration ref 'g<id>')."""
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO guidance_log (job_id, stage, action, text, via, artifact_sha, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, stage, action, text, via, artifact_sha, time.time()),
            )
            gid = cur.lastrowid
            # Epic D4: human decisions are retrievable context. gate_events /
            # admin_events are deliberately NEVER indexed (refusals and
            # escalations must never reach model context as human decisions).
            if action in ("proceed", "redo", "answer", "chat", "steer") and (text or "").strip():
                row = c.execute("SELECT project, workspace_id FROM jobs WHERE issue_id = ?",
                                (job_id,)).fetchone()
                self._fts_upsert(
                    c, "guidance", f"g{gid}",
                    project=(row["project"] if row else "") or "",
                    workspace_id=row["workspace_id"] if row else None,
                    job_id=job_id, title=f"P{stage} {action}", body=text)
            return gid

    def guidance_for(self, job_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM guidance_log WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- FTS memory retrieval (Epic D4, docs/ENGINE.md §16) ----------
    # Indexed: active decisions, human guidance, live artifacts, memory files.
    # Deliberately NOT indexed: gate_events/admin_events (refusals must never
    # read as human decisions), candidate/dismissed/superseded decisions, and
    # artifacts flagged 'superseded' (the engine banners them "will be
    # regenerated"). Absence of FTS5 degrades retrieval to absent — additive
    # prompt context only, no control flow depends on it.

    def _fts_upsert(self, c, kind: str, key: str, *, project: str = "",
                    workspace_id=None, scope: str = "", job_id: str = "",
                    path: str = "", title: str = "", body: str = ""):
        """DELETE+INSERT on an open connection (fts5 has no ON CONFLICT).
        No-op when FTS is unavailable or the body is blank."""
        if not self.fts_enabled:
            return
        body = (body or "").strip()
        if not body:
            return
        c.execute("DELETE FROM mem_fts WHERE kind = ? AND key = ?", (kind, key))
        c.execute(
            "INSERT INTO mem_fts (kind, key, project, workspace_id, scope, "
            "job_id, path, title, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, key, project or "",
             "" if workspace_id is None else str(int(workspace_id)),
             scope or "", job_id or "", path or "", (title or "")[:300],
             body[:FTS_BODY_MAX]),
        )

    def _fts_delete(self, c, kind: str, key: str):
        if not self.fts_enabled:
            return
        c.execute("DELETE FROM mem_fts WHERE kind = ? AND key = ?", (kind, key))

    def fts_upsert(self, kind: str, key: str, **kwargs):
        """Standalone upsert (memory.refresh_cache uses this)."""
        if not self.fts_enabled:
            return
        with self._conn() as c:
            self._fts_upsert(c, kind, key, **kwargs)

    def fts_delete(self, kind: str, key: str):
        if not self.fts_enabled:
            return
        with self._conn() as c:
            self._fts_delete(c, kind, key)

    def fts_has(self, kind: str, project: str) -> bool:
        """Any rows of a kind for a project? (cache-warm check for reindexing)."""
        if not self.fts_enabled:
            return False
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM mem_fts WHERE kind = ? AND project = ? LIMIT 1",
                (kind, project)).fetchone() is not None

    def fts_search(self, terms: list[str], *, project: str = "",
                   workspace_id=None, exclude_job_id: str = "",
                   kinds: tuple = (), limit: int = 5) -> list[dict]:
        """Ranked snippet search for prompt assembly. Scoping is an explicit
        per-kind whitelist (never one shared OR-chain — a project='' admission
        must not leak workspace-scoped decisions into every run):
        - guidance/artifact: exact project match (project-less rows excluded),
          never the asking job's own rows;
        - memory: exact project match;
        - decision: workspace match OR scope='org' (candidates are simply not
          in the index), never the asking job's own rows.
        A malformed query must never fail a stage run → [] on any FTS error."""
        if not self.fts_enabled:
            return []
        clean: list[str] = []
        for t in terms or []:
            for part in _FTS_TERM_RE.sub(" ", str(t or "")).split():
                if part and part not in clean:
                    clean.append(part)
        if not clean:
            return []  # MATCH '' raises — an empty term list is 'no results'
        match = " OR ".join(f'"{t}"' for t in clean)
        ws = "" if workspace_id is None else str(int(workspace_id))
        excl = exclude_job_id or ""
        # exclude rows of the asking job ONLY when an exclusion is requested —
        # '' must not accidentally exclude job-less rows (manual decisions)
        not_own = "(? = '' OR job_id != ?)"
        where = ("("
                 f"(kind IN ('guidance','artifact') AND project != '' "
                 f" AND project = ? AND {not_own})"
                 " OR (kind = 'memory' AND project = ?)"
                 " OR (kind = 'decision' AND (scope = 'org' OR (? != '' AND workspace_id = ?))"
                 f"     AND {not_own})"
                 ")")
        params: list = [match, project or "", excl, excl,
                        project or "", ws, ws, excl, excl]
        kind_clause = ""
        if kinds:
            marks = ",".join("?" for _ in kinds)
            kind_clause = f" AND kind IN ({marks})"
            params.extend(kinds)
        params.append(int(limit))
        try:
            with self._conn() as c:
                rows = c.execute(
                    f"""SELECT kind, key, project, path, title,
                               snippet(mem_fts, 8, '', '', ' … ', 24) AS snippet
                        FROM mem_fts
                        WHERE mem_fts MATCH ? AND {where}{kind_clause}
                        ORDER BY bm25(mem_fts) LIMIT ?""",
                    params).fetchall()
                return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []

    # ---------- decision registry (Epic D2, docs/ENGINE.md §16) ----------

    def decision_add(self, source: str, text: str, *, ref: str = "",
                     status: str = "active", scope: str = "job",
                     job_id: str = "", workspace_id=None, project: str = "",
                     stage: int | None = None, title: str = "",
                     decided_by: str = "", links="[]",
                     origin_author: str = "") -> int | None:
        """INSERT OR IGNORE keyed on the partial UNIQUE (source, ref) index —
        idempotent under any replay; a previously DISMISSED candidate's ref
        row still exists, so it can never be re-created. Returns the new row
        id, or None when deduped — detected via cur.rowcount, NEVER lastrowid
        (an ignored insert leaves lastrowid at the previous insert's id).
        Empty text is refused loudly (same posture as gate_event_add).
        Active rows enter the FTS index in the same transaction; candidates
        never do."""
        text = (text or "").strip()
        if not text:
            raise ValueError("decision_add requires non-empty text")
        text = text[:DECISION_TEXT_MAX]
        if isinstance(links, (list, tuple)):
            links = json.dumps([str(l) for l in links])
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO decisions
                     (source, status, scope, job_id, workspace_id, project, stage,
                      title, text, decided_by, origin_author, links, ref,
                      created_at, updated_at, updated_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (source, status, scope, job_id, workspace_id, project, stage,
                 (title or "")[:200], text, decided_by, origin_author,
                 links or "[]", (ref or "").strip(), now, now, decided_by),
            )
            if cur.rowcount == 0:
                return None  # deduped by (source, ref)
            did = cur.lastrowid
            if status == "active":
                self._fts_upsert(c, "decision", f"d{did}", project=project,
                                 workspace_id=workspace_id, scope=scope,
                                 job_id=job_id, path=f"decisions/#{did}",
                                 title=title or f"{scope} decision",
                                 body=f"{title}\n{text}".strip())
            return did

    def decision_get(self, decision_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM decisions WHERE id = ?",
                            (int(decision_id),)).fetchone()
            return dict(row) if row else None

    DECISION_EDIT_FIELDS = ("scope", "title", "text", "links", "decided_by",
                            "project", "stage")

    def decision_set_status(self, decision_id: int, from_statuses: list[str],
                            to_status: str, updated_by: str, **fields) -> bool:
        """CAS on status — the single-writer guard for confirm/dismiss/
        supersede races (one UPDATE, WHERE status IN (...)). Returns True iff
        this caller won. Syncs the FTS row: delete on leaving 'active',
        insert on entering it. Extra fields (confirm-with-edits) are
        whitelisted; callers validate values first."""
        fields = {k: v for k, v in fields.items() if k in self.DECISION_EDIT_FIELDS}
        if "text" in fields:
            fields["text"] = str(fields["text"] or "")[:DECISION_TEXT_MAX]
        if "title" in fields:
            fields["title"] = str(fields["title"] or "")[:200]
        if "links" in fields and isinstance(fields["links"], (list, tuple)):
            fields["links"] = json.dumps([str(l) for l in fields["links"]])
        marks = ",".join("?" for _ in from_statuses)
        cols = "".join(f", {k} = ?" for k in fields)
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE decisions SET status = ?, updated_at = ?, updated_by = ?"
                f"{cols} WHERE id = ? AND status IN ({marks})",
                (to_status, time.time(), updated_by, *fields.values(),
                 int(decision_id), *from_statuses),
            )
            if cur.rowcount != 1:
                return False
            row = c.execute("SELECT * FROM decisions WHERE id = ?",
                            (int(decision_id),)).fetchone()
            if to_status == "active" and row:
                self._fts_upsert(c, "decision", f"d{decision_id}",
                                 project=row["project"],
                                 workspace_id=row["workspace_id"],
                                 scope=row["scope"], job_id=row["job_id"],
                                 path=f"decisions/#{decision_id}",
                                 title=row["title"] or f"{row['scope']} decision",
                                 body=f"{row['title']}\n{row['text']}".strip())
            else:
                self._fts_delete(c, "decision", f"d{decision_id}")
            return True

    @staticmethod
    def _like_escape(q: str) -> str:
        return (q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))

    def decisions_query(self, *, q: str = "", scope: str = "", status: str = "",
                        project: str = "", source: str = "",
                        workspace_ids: list[int] | None = None,
                        limit: int = 100, offset: int = 0) -> list[dict]:
        """Registry view. workspace_ids=None = admin (all rows); a list =
        member visibility — EXACTLY the prompt-admission predicate: rows of
        the member's workspaces plus scope='org' rows (which reach every
        member's prompts, so every member may see and read them); [] = org
        rows only. Default status view excludes candidates AND dismissed
        (inbox material / remembered rejections, not registry truth).
        q is a LIKE filter with %/_ escaped (user-supplied search text) —
        deliberately not FTS: the index excludes non-active rows, and a
        status-filtered registry search must not depend on it."""
        where, params = [], []
        if scope:
            where.append("scope = ?"); params.append(scope)
        if status:
            where.append("status = ?"); params.append(status)
        else:
            where.append("status IN ('active', 'superseded')")
        if project:
            where.append("project = ?"); params.append(project)
        if source:
            where.append("source = ?"); params.append(source)
        if workspace_ids is not None:
            if workspace_ids:
                marks = ",".join("?" for _ in workspace_ids)
                where.append(f"(scope = 'org' OR workspace_id IN ({marks}))")
                params.extend(int(w) for w in workspace_ids)
            else:
                where.append("scope = 'org'")
        if q.strip():
            esc = f"%{self._like_escape(q.strip())}%"
            where.append("(title LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\')")
            params.extend([esc, esc])
        clause = " AND ".join(where) if where else "1=1"
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM decisions WHERE {clause} "
                f"ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, int(limit), int(offset))).fetchall()
            return [dict(r) for r in rows]

    def decisions_for_job(self, job_id: str) -> list[dict]:
        """This feature's own ACTIVE decisions — the P9 registry feed.
        Superseded rows (incl. a re-intaken lap's, cleared by
        _clear_pipeline_state) never resurface here."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM decisions WHERE job_id = ? AND status = 'active' "
                "ORDER BY id", (job_id,)).fetchall()
            return [dict(r) for r in rows]

    def decisions_recent_scoped(self, workspace_id: int, scopes: tuple,
                                limit: int = 10) -> list[dict]:
        """Recent active decisions for a prompt: org-scope rows are admitted
        regardless of workspace_id; every other scope requires an exact
        workspace match (callers skip this entirely when the job has no
        stamped workspace — fail closed)."""
        marks = ",".join("?" for _ in scopes)
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT * FROM decisions
                    WHERE status = 'active' AND scope IN ({marks})
                      AND (scope = 'org' OR workspace_id = ?)
                    ORDER BY id DESC LIMIT ?""",
                (*scopes, int(workspace_id), int(limit))).fetchall()
            return [dict(r) for r in rows]

    def decision_candidates(self, workspace_ids: list[int] | None,
                            limit: int = 50) -> list[dict]:
        """Parked Slack candidates for the inbox — oldest first, membership-
        scoped (None = admin/all; [] = none: candidates are always workspace-
        routed, so there is no org admission here)."""
        with self._conn() as c:
            if workspace_ids is None:
                rows = c.execute(
                    "SELECT * FROM decisions WHERE status = 'candidate' "
                    "ORDER BY id LIMIT ?", (int(limit),)).fetchall()
            elif not workspace_ids:
                rows = []
            else:
                marks = ",".join("?" for _ in workspace_ids)
                rows = c.execute(
                    f"SELECT * FROM decisions WHERE status = 'candidate' "
                    f"AND workspace_id IN ({marks}) ORDER BY id LIMIT ?",
                    (*[int(w) for w in workspace_ids], int(limit))).fetchall()
            return [dict(r) for r in rows]

    # ---------- Slack ingest cursors (Epic D3) ----------

    def slack_cursor_get(self, channel: str) -> str | None:
        """The channel's watermark, or None when the channel was never
        initialized (the loop initializes to NOW and skips — forward-only)."""
        with self._conn() as c:
            row = c.execute("SELECT last_ts FROM slack_cursors WHERE channel = ?",
                            (channel,)).fetchone()
            return row["last_ts"] if row else None

    def slack_cursor_set(self, channel: str, ts: str):
        with self._conn() as c:
            c.execute(
                """INSERT INTO slack_cursors (channel, last_ts, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(channel) DO UPDATE SET
                     last_ts = excluded.last_ts, updated_at = excluded.updated_at""",
                (channel, str(ts), time.time()))

    def slack_cursor_init(self, channel: str, ts: str):
        """First-allowlist initialization: INSERT OR IGNORE — an existing
        watermark (channel re-added) is never moved."""
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO slack_cursors (channel, last_ts, updated_at) "
                "VALUES (?, ?, ?)", (channel, str(ts), time.time()))

    # ---------- gate events (Epic A: audit substrate + idempotence store) ----------
    # NOT guidance_log: guidance renders into stage prompts, and refusals /
    # escalations must never leak into model context as "human decisions".

    def gate_event_add(self, job_id: str, kind: str, ref: str, stage: int | None = None,
                       detail: str = "", actor: str = "") -> bool:
        """Record a gate event, idempotently keyed on (job_id, kind, ref).
        Returns True only when the row is NEW — callers key one-time actions
        (refusal replies, escalation sends) off that. An empty ref would make
        any two same-kind events silently dedupe — refused loudly."""
        ref = str(ref or "").strip()
        if not ref:
            raise ValueError("gate_event_add requires a non-empty ref")
        with self._conn() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO gate_events (job_id, stage, kind, ref, detail, actor, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (job_id, stage, kind, ref, detail, actor, time.time()),
            )
            return cur.rowcount == 1

    def admin_event_add(self, kind: str, target: str = "", detail: str = "",
                        actor: str = ""):
        """Append-only admin/config mutation audit (see the admin_events DDL).
        Callers redact secret values BEFORE they reach detail."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO admin_events (kind, target, detail, actor, at) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, target, detail, actor, time.time()),
            )

    def admin_events_recent(self, limit: int = 100) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM admin_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def gate_events_for(self, job_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM gate_events WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- inbox items (Epic I0: durable routine outputs) ----------

    def inbox_item_add(self, kind: str, dedupe_key: str, title: str,
                       body: str = "", refs: dict | str | None = None,
                       workspace_id: int | None = None, source: str = "",
                       source_sig: str = "") -> bool:
        """INSERT OR IGNORE keyed on UNIQUE(kind, dedupe_key) — returns True
        only when the row is NEW (callers key Slack sends off it, exactly like
        gate_event_add). The same index is the DISMISSAL MEMORY: a dismissed
        key blocks re-insert forever. Empty dedupe_key is refused loudly."""
        dedupe_key = str(dedupe_key or "").strip()
        if not dedupe_key:
            raise ValueError("inbox_item_add requires a non-empty dedupe_key")
        if isinstance(refs, dict):
            refs = json.dumps(refs)
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO inbox_items
                     (workspace_id, kind, source, dedupe_key, source_sig, title,
                      body, refs, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                (workspace_id, kind, source, dedupe_key, source_sig,
                 (title or "")[:300], body or "", refs or "{}", now, now),
            )
            return cur.rowcount == 1

    def inbox_item_get(self, item_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM inbox_items WHERE id = ?",
                            (int(item_id),)).fetchone()
            return dict(row) if row else None

    def inbox_items_open(self, workspace_ids: list[int] | None,
                         kinds: tuple = (), limit: int = 200) -> list[dict]:
        """Open notices, membership-scoped: None = all rows incl. instance-wide
        NULL-workspace ones (admins); a list = only those workspaces (members
        never see NULL-workspace rows); [] = none."""
        where, params = ["status = 'open'"], []
        if workspace_ids is not None:
            if not workspace_ids:
                return []
            marks = ",".join("?" for _ in workspace_ids)
            where.append(f"workspace_id IN ({marks})")
            params.extend(int(w) for w in workspace_ids)
        if kinds:
            marks = ",".join("?" for _ in kinds)
            where.append(f"kind IN ({marks})")
            params.extend(kinds)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM inbox_items WHERE {' AND '.join(where)} "
                f"ORDER BY id DESC LIMIT ?", (*params, int(limit))).fetchall()
            return [dict(r) for r in rows]

    def inbox_item_resolve(self, item_id: int, to_status: str, by: str,
                           extra_refs: dict | None = None) -> bool:
        """CAS on the ONLY state transition these rows have: open → dismissed/
        adopted/expired. Returns True iff this caller won (the loser of a
        dismiss/adopt race gets a 409 upstream). The row itself (status_by/
        status_at, never deleted) is the audit record. extra_refs merges into
        the stored refs JSON."""
        now = time.time()
        with self._conn() as c:
            row = c.execute("SELECT refs FROM inbox_items WHERE id = ?",
                            (int(item_id),)).fetchone()
            refs = "{}" if row is None else (row["refs"] or "{}")
            if extra_refs:
                try:
                    merged = json.loads(refs)
                    if not isinstance(merged, dict):
                        merged = {}
                except (ValueError, TypeError):
                    merged = {}
                merged.update(extra_refs)
                refs = json.dumps(merged)
            cur = c.execute(
                """UPDATE inbox_items SET status = ?, status_by = ?, status_at = ?,
                     updated_at = ?, refs = ?
                   WHERE id = ? AND status = 'open'""",
                (to_status, by, now, now, refs, int(item_id)))
            return cur.rowcount == 1

    def inbox_item_merge_refs(self, item_id: int, extra_refs: dict):
        """Merge refs on an already-resolved row (e.g. recording an intake
        error on an adopted proposal — visible, auditable, never a silent
        un-adopt)."""
        with self._conn() as c:
            row = c.execute("SELECT refs FROM inbox_items WHERE id = ?",
                            (int(item_id),)).fetchone()
            if row is None:
                return
            try:
                merged = json.loads(row["refs"] or "{}")
                if not isinstance(merged, dict):
                    merged = {}
            except (ValueError, TypeError):
                merged = {}
            merged.update(extra_refs)
            c.execute("UPDATE inbox_items SET refs = ?, updated_at = ? WHERE id = ?",
                      (json.dumps(merged), time.time(), int(item_id)))

    def inbox_items_expire(self, kinds: tuple, older_than_days: float) -> int:
        """Flip stale OPEN rows of the given kinds to 'expired' (visible aging,
        never silent deletion). Dismissed/adopted rows are never touched — the
        dismissal memory must persist."""
        if not kinds:
            return 0
        cutoff = time.time() - float(older_than_days) * 86400
        marks = ",".join("?" for _ in kinds)
        with self._conn() as c:
            cur = c.execute(
                f"""UPDATE inbox_items SET status = 'expired', status_by = 'engine',
                      status_at = ?, updated_at = ?
                    WHERE status = 'open' AND kind IN ({marks}) AND created_at < ?""",
                (time.time(), time.time(), *kinds, cutoff))
            return cur.rowcount

    def inbox_expire_predecessors(self, kind: str, workspace_id: int | None,
                                  before_id: int) -> int:
        """A fresh digest/pack expires its still-open predecessors directly
        (status_by='engine') so counts.notices never grows monotonically."""
        with self._conn() as c:
            cur = c.execute(
                """UPDATE inbox_items SET status = 'expired', status_by = 'engine',
                     status_at = ?, updated_at = ?
                   WHERE status = 'open' AND kind = ? AND id < ?
                     AND (workspace_id = ? OR (workspace_id IS NULL AND ? IS NULL))""",
                (time.time(), time.time(), kind, int(before_id),
                 workspace_id, workspace_id))
            return cur.rowcount

    def inbox_item_recent_sig(self, kind: str, source_sig: str,
                              since: float) -> bool:
        """Any item (ANY status — dismissal memory included) of this kind with
        the same coarse source signature newer than `since`? The proposal
        recency guard: a dismissed friction brief holds until the pain
        measurably grows AND the window has passed."""
        if not source_sig:
            return False
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM inbox_items WHERE kind = ? AND source_sig = ? "
                "AND created_at >= ? LIMIT 1",
                (kind, source_sig, since)).fetchone() is not None

    # ---------- frictions (Epic I5: engine data, not just a mirror) ----------

    def friction_add(self, job_id: str, workspace_id: int | None, project: str,
                     stage: int | None, source: str, text: str):
        text = (text or "").strip()
        if not text:
            return
        with self._conn() as c:
            c.execute(
                "INSERT INTO frictions (job_id, workspace_id, project, stage, "
                "source, text, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, workspace_id, project or "", stage, source,
                 text[:500], time.time()))

    def frictions_since(self, since: float,
                        workspace_id: int | None = None) -> list[dict]:
        with self._conn() as c:
            if workspace_id is None:
                rows = c.execute(
                    "SELECT * FROM frictions WHERE at >= ? ORDER BY id",
                    (since,)).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM frictions WHERE at >= ? AND workspace_id = ? "
                    "ORDER BY id", (since, int(workspace_id))).fetchall()
            return [dict(r) for r in rows]

    # ---------- routines (Epic I1: the routine engine) ----------

    def routine_upsert_seed(self, kind: str, workspace_id: int | None,
                            schedule: str, name: str = "",
                            enabled: bool = True) -> bool:
        """Seed a routine row — INSERT OR IGNORE on the (kind, workspace)
        unique index, so seeding NEVER overwrites operator edits. Returns True
        when the row is new. Distinct from the boot-time last_run_at bump
        (routine_boot_bump), which runs at EVERY boot."""
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO routines
                     (workspace_id, kind, name, schedule, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (workspace_id, kind, name or kind, schedule, int(enabled), now, now))
            return cur.rowcount == 1

    def routines_all(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM routines ORDER BY workspace_id IS NOT NULL, "
                "workspace_id, kind").fetchall()
            return [dict(r) for r in rows]

    def routine_get(self, routine_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM routines WHERE id = ?",
                            (int(routine_id),)).fetchone()
            return dict(row) if row else None

    def routine_set(self, routine_id: int, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE routines SET {cols}, updated_at = ? WHERE id = ?",
                      (*fields.values(), time.time(), int(routine_id)))

    def routine_claim(self, routine_id: int, prev_last_run: float | None,
                      now: float, ignore_disabled: bool = False) -> bool:
        """Single-flight claim CAS: exactly one caller wins each due firing —
        correct today and load-bearing under Epic F2 multi-worker.
        ignore_disabled=True is the reaper's non-disableable escape."""
        enabled_clause = "" if ignore_disabled else " AND enabled = 1"
        with self._conn() as c:
            cur = c.execute(
                f"""UPDATE routines SET last_run_at = ?, updated_at = ?
                    WHERE id = ? AND (last_run_at IS ? OR last_run_at = ?)
                    {enabled_clause}""",
                (now, now, int(routine_id), prev_last_run, prev_last_run))
            return cur.rowcount == 1

    def routine_boot_bump(self, kinds: tuple, now: float | None = None):
        """Boot-time settle bump for builtin (instance-scoped) rows: stamp
        last_run_at=now so sweep/janitor fire one full interval after boot
        instead of ~immediately off a stale pre-restart stamp. Runs at EVERY
        boot — deliberately NOT part of the INSERT OR IGNORE seeding."""
        if not kinds:
            return
        marks = ",".join("?" for _ in kinds)
        with self._conn() as c:
            c.execute(
                f"UPDATE routines SET last_run_at = ?, updated_at = ? "
                f"WHERE workspace_id IS NULL AND kind IN ({marks})",
                (now or time.time(), time.time(), *kinds))

    def routine_run_open(self, routine_id: int, kind: str,
                         workspace_id: int | None) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO routine_runs (routine_id, kind, workspace_id, started_at) "
                "VALUES (?, ?, ?, ?)",
                (int(routine_id), kind, workspace_id, time.time()))
            return cur.lastrowid

    def routine_run_close(self, run_id: int, status: str, detail: str = "",
                          items_emitted: int = 0):
        with self._conn() as c:
            c.execute(
                "UPDATE routine_runs SET ended_at = ?, status = ?, detail = ?, "
                "items_emitted = ? WHERE id = ? AND ended_at IS NULL",
                (time.time(), status, (detail or "")[:1000],
                 int(items_emitted), int(run_id)))

    def routine_runs_recent(self, routine_id: int, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM routine_runs WHERE routine_id = ? "
                "ORDER BY id DESC LIMIT ?", (int(routine_id), int(limit))).fetchall()
            return [dict(r) for r in rows]

    def routine_last_success(self, routine_id: int) -> float | None:
        """When the routine last completed usefully (ok OR quiet) — the digest
        `since` anchor (Epic I2, floored at now-24h by the caller)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(started_at) AS t FROM routine_runs "
                "WHERE routine_id = ? AND status IN ('ok', 'quiet')",
                (int(routine_id),)).fetchone()
            return row["t"] if row and row["t"] else None

    def routine_runs_prune(self, ttl_days: float, keep_latest: int = 20) -> int:
        """Janitor retention: drop run-history rows older than the TTL, always
        keeping the newest `keep_latest` per routine."""
        cutoff = time.time() - float(ttl_days) * 86400
        with self._conn() as c:
            cur = c.execute(
                """DELETE FROM routine_runs WHERE started_at < ? AND id NOT IN (
                     SELECT id FROM routine_runs r2
                     WHERE r2.routine_id = routine_runs.routine_id
                     ORDER BY r2.id DESC LIMIT ?)""",
                (cutoff, int(keep_latest)))
            return cur.rowcount

    # ---------- spend (Epic I0/I4; the substrate Epic G4 extends) ----------

    def costs_since(self, since: float) -> dict:
        """workspace_id -> total USD since `since`, aggregating stage_runs AND
        gate_chat costs through the owning job's workspace. Jobs without a
        workspace land under None."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT j.workspace_id AS ws, SUM(x.c) AS total FROM (
                     SELECT job_id, COALESCE(cost_usd, 0) AS c FROM stage_runs
                       WHERE started_at >= ?
                     UNION ALL
                     SELECT job_id, COALESCE(cost_usd, 0) FROM gate_chat
                       WHERE at >= ?
                   ) x JOIN jobs j ON j.issue_id = x.job_id
                   GROUP BY j.workspace_id""", (since, since)).fetchall()
            return {r["ws"]: float(r["total"] or 0) for r in rows}

    def awaiting_gates(self) -> list[dict]:
        """Everything answerable right now: parked gates (all kinds) plus
        feature jobs in error/timeout (redo is a valid answer there)."""
        rows = self.by_status(["awaiting_input", "error", "timeout"])
        return [r for r in rows
                if r["status"] == "awaiting_input" or (r.get("kind") or "") == "feature"]

    # ---------- artifact sync state (row per artifact) ----------

    def artifact_get(self, job_id: str, artifact: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM artifact_state WHERE job_id = ? AND artifact = ?",
                (job_id, artifact),
            ).fetchone()
            return dict(row) if row else None

    def artifact_set(self, job_id: str, artifact: str, **fields):
        current = self.artifact_get(job_id, artifact) or {
            "subtask_id": "", "synced_hash": "", "flags": "", "content": ""
        }
        keys = ("subtask_id", "synced_hash", "flags", "content")
        merged = {**{k: current.get(k) or "" for k in keys}, **fields}
        with self._conn() as c:
            c.execute(
                """INSERT INTO artifact_state (job_id, artifact, subtask_id, synced_hash, flags, content)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id, artifact) DO UPDATE SET
                     subtask_id = excluded.subtask_id,
                     synced_hash = excluded.synced_hash,
                     flags = excluded.flags,
                     content = excluded.content""",
                (job_id, artifact, merged["subtask_id"], merged["synced_hash"],
                 merged["flags"], merged["content"]),
            )
            # Epic D4: the index refreshes on every artifact write — and drops
            # rows the engine itself banners SUPERSEDED ("will be regenerated;
            # edits ignored") the moment the flag lands. Both artifact write
            # paths (commit_file + the human-edit pull) funnel through here.
            if self.fts_enabled and ("content" in fields or "flags" in fields):
                if "superseded" in (merged["flags"] or ""):
                    self._fts_delete(c, "artifact", f"{job_id}/{artifact}")
                elif (merged["content"] or "").strip():
                    row = c.execute(
                        "SELECT project, workspace_id FROM jobs WHERE issue_id = ?",
                        (job_id,)).fetchone()
                    self._fts_upsert(
                        c, "artifact", f"{job_id}/{artifact}",
                        project=(row["project"] if row else "") or "",
                        workspace_id=row["workspace_id"] if row else None,
                        job_id=job_id, path=f"features/{job_id}/{artifact}",
                        title=artifact, body=merged["content"])

    def artifact_content_set(self, job_id: str, artifact: str, content: str):
        """Cache the artifact body for the fast-lane chat bundle (truncated)."""
        content = content or ""
        if len(content) > ARTIFACT_CONTENT_MAX:
            content = content[:ARTIFACT_CONTENT_MAX] + "\n… (truncated cache)"
        self.artifact_set(job_id, artifact, content=content)

    def artifact_contents(self, job_id: str, names: list[str]) -> dict[str, str]:
        """name -> cached body, for the requested artifacts that have one."""
        marks = ",".join("?" for _ in names)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT artifact, content FROM artifact_state "
                f"WHERE job_id = ? AND artifact IN ({marks})",
                (job_id, *names),
            ).fetchall()
            return {r["artifact"]: r["content"] for r in rows if (r["content"] or "").strip()}

    def artifacts_for(self, job_id: str) -> list[dict]:
        """Sync/telemetry view — excludes the cached body (it would bloat the
        stats API; fetch bodies via artifact_contents)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT job_id, artifact, subtask_id, synced_hash, flags "
                "FROM artifact_state WHERE job_id = ? ORDER BY artifact", (job_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def artifacts_clear(self, job_id: str):
        """Full pipeline-state reset for a fresh restart. Keeps stage_runs (telemetry
        is history) but clears guidance so a dead pipeline's redo notes can't leak
        into the new run as binding corrections."""
        with self._conn() as c:
            self._clear_pipeline_state(c, job_id)

    def _clear_pipeline_state(self, c, job_id: str):
        """Runs on the CALLER's open connection so the whole reset commits
        atomically. The same fail-closed rationale that clears guidance_log
        extends to Epic D: the dead lap's registry rows are superseded (never
        re-injected into the new lap's P9 as registry truth) and its FTS
        guidance/artifact/decision rows are purged in the SAME transaction —
        a dead pipeline's text must not haunt retrieval."""
        c.execute("DELETE FROM artifact_state WHERE job_id = ?", (job_id,))
        c.execute("DELETE FROM stage_state WHERE job_id = ?", (job_id,))
        c.execute("DELETE FROM guidance_log WHERE job_id = ?", (job_id,))
        c.execute(
            "UPDATE decisions SET status = 'superseded', updated_at = ?, "
            "updated_by = 'engine:reintake' WHERE job_id = ? AND status = 'active'",
            (time.time(), job_id))
        if self.fts_enabled:
            c.execute("DELETE FROM mem_fts WHERE kind IN "
                      "('guidance', 'artifact', 'decision') AND job_id = ?",
                      (job_id,))

    def feature_intake(self, job_id: str, title: str, project: str, **fields):
        """Atomic feature (re-)intake: child-state clears + row upsert + pipeline
        reset in ONE transaction — a crash can never leave status='received' with a
        stale stage and no stage_state (which would strand the job on restart).

        The previous lap's outcome-watch state resets in the SAME transaction
        (Epic B): a stale `watch-<job>` row would block the fresh lap's spawn
        forever (idempotent-by-existence), and its readings/ledger row would mix
        two laps' measurements. The new lap is measured from scratch."""
        now = time.time()
        watch_id = f"watch-{job_id}"
        with self._conn() as c:
            self._clear_pipeline_state(c, job_id)
            c.execute("DELETE FROM jobs WHERE issue_id = ? AND kind = 'watch'", (watch_id,))
            c.execute("DELETE FROM metric_readings WHERE job_id = ?", (watch_id,))
            c.execute("DELETE FROM outcomes WHERE job_id = ?", (watch_id,))
            c.execute(
                """INSERT INTO jobs (issue_id, kind, project, title, status, source, forced, attempts, created_at, updated_at)
                   VALUES (?, 'feature', ?, ?, 'received', 'manual', 1, 1, ?, ?)
                   ON CONFLICT(issue_id) DO UPDATE SET
                     status = 'received',
                     source = 'manual',
                     forced = 1,
                     title = excluded.title,
                     project = excluded.project,
                     attempts = jobs.attempts + 1,
                     updated_at = excluded.updated_at""",
                (job_id, project, title, now, now),
            )
            if fields:
                cols = ", ".join(f"{k} = ?" for k in fields)
                c.execute(
                    f"UPDATE jobs SET {cols}, updated_at = ? WHERE issue_id = ?",
                    (*fields.values(), now, job_id),
                )

    # ---------- outcome watch (Epic B4/B5) ----------

    def watch_insert(self, job_id: str, **fields):
        """Spawn a watch job in ONE transaction: the row is born kind='watch'
        AND status='watching' — never insert()+set_status, whose intermediate
        'received' state would be re-enqueued at boot and dispatched into a
        Claude run. Raises sqlite3.IntegrityError if the row already exists
        (callers check store.get first; a race fails loudly, not twice)."""
        now = time.time()
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._conn() as c:
            c.execute(
                f"INSERT INTO jobs (issue_id, kind, status, source, forced, attempts"
                f"{', ' + cols if cols else ''}, created_at, updated_at) "
                f"VALUES (?, 'watch', 'watching', 'manual', 1, 1"
                f"{', ' + marks if marks else ''}, ?, ?)",
                (job_id, *fields.values(), now, now),
            )

    def reading_add(self, job_id: str, metric: str, metric_event: str,
                    observed: float | None, window_day: int | None,
                    detail: str = "", window_start: float | None = None):
        """One successful metric read (INSERT-only). window_start stamps which
        watch window (watch_started_at) the reading belongs to — a /redo arms a
        new window and must never mix readings with the old one."""
        with self._conn() as c:
            c.execute(
                """INSERT INTO metric_readings
                     (job_id, metric, metric_event, observed, window_day, window_start, detail, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, metric, metric_event, observed, window_day, window_start,
                 detail, time.time()),
            )

    def readings_for(self, job_id: str, window_start: float | None = None) -> list[dict]:
        with self._conn() as c:
            if window_start is None:
                rows = c.execute(
                    "SELECT * FROM metric_readings WHERE job_id = ? ORDER BY id",
                    (job_id,)).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM metric_readings WHERE job_id = ? AND window_start = ? ORDER BY id",
                    (job_id, window_start)).fetchall()
            return [dict(r) for r in rows]

    def reading_last_at(self, job_id: str) -> float:
        """When the last reading landed (any window) — the daily-read throttle."""
        with self._conn() as c:
            row = c.execute("SELECT MAX(at) AS t FROM metric_readings WHERE job_id = ?",
                            (job_id,)).fetchone()
            return (row["t"] if row and row["t"] else 0) or 0

    # measurement/verdict fields the finish path may (re)write — pinned so a
    # replayed or post-redo _finish_watch can NEVER clobber the human's
    # learning/decided_by/decided_at (audit-ledger integrity)
    OUTCOME_VERDICT_FIELDS = ("metric", "metric_event", "target", "observed",
                              "baseline", "window_days", "verdict", "verdict_inputs")

    def outcome_add(self, job_id: str, feature_id: str, workspace_id: int | None,
                    **fields):
        """UPSERT the ledger row for a finished watch. ONE semantics: verdict/
        measurement fields update in place (a /redo re-finish records the new
        verdict), created_at survives, and learning/decided_* are untouchable
        here — they belong to outcome_set (the human's answer)."""
        fields = {k: v for k, v in fields.items() if k in self.OUTCOME_VERDICT_FIELDS}
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        sets = ", ".join(f"{k} = excluded.{k}" for k in fields)
        with self._conn() as c:
            c.execute(
                f"INSERT INTO outcomes (job_id, feature_id, workspace_id"
                f"{', ' + cols if cols else ''}, created_at) "
                f"VALUES (?, ?, ?{', ' + marks if marks else ''}, ?) "
                f"ON CONFLICT(job_id) DO UPDATE SET "
                f"feature_id = excluded.feature_id, workspace_id = excluded.workspace_id"
                f"{', ' + sets if sets else ''}",
                (job_id, feature_id, workspace_id, *fields.values(), time.time()),
            )

    def outcome_set(self, job_id: str, **fields):
        """The human's side of the ledger row (learning/decided_by/decided_at)."""
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE outcomes SET {cols} WHERE job_id = ?",
                      (*fields.values(), job_id))

    def outcome_for(self, job_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM outcomes WHERE job_id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def outcome_for_feature(self, feature_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM outcomes WHERE feature_id = ? ORDER BY id DESC LIMIT 1",
                (feature_id,)).fetchone()
            return dict(row) if row else None

    def outcomes_recent(self, limit: int = 200) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM outcomes ORDER BY id DESC LIMIT ?",
                             (limit,)).fetchall()
            return [dict(r) for r in rows]

    # ---------- per-stage state ----------

    def stage_state_get(self, job_id: str, stage: int) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM stage_state WHERE job_id = ? AND stage = ?", (job_id, stage)
            ).fetchone()
            return dict(row) if row else None

    def stage_state_set(self, job_id: str, stage: int, base_sha: str | None = None,
                        bump_attempts: bool = False):
        with self._conn() as c:
            c.execute(
                """INSERT INTO stage_state (job_id, stage, base_sha, attempts)
                   VALUES (?, ?, COALESCE(?, ''), ?)
                   ON CONFLICT(job_id, stage) DO UPDATE SET
                     base_sha = COALESCE(?, stage_state.base_sha),
                     attempts = stage_state.attempts + ?""",
                (job_id, stage, base_sha, 1 if bump_attempts else 0,
                 base_sha, 1 if bump_attempts else 0),
            )

    # ---------- stage run telemetry (the 10x receipts) ----------

    def stage_run_open(self, job_id: str, stage: int, attempt: int,
                       queued_at: float | None = None) -> int:
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO stage_runs (job_id, stage, attempt, queued_at, started_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, stage, attempt, queued_at or now, now),
            )
            return cur.lastrowid

    def stage_run_close(self, run_id: int, result_status: str,
                        cost_usd: float | None = None, num_turns: int | None = None,
                        duration_ms: float | None = None, session_id: str | None = None):
        with self._conn() as c:
            # first close wins: an already-closed run keeps its status, so a
            # late exception in the same flow (e.g. run_stage's generic handler
            # firing after _steer_reenqueue already closed the run 'interrupted')
            # can't overwrite good telemetry with 'exception'.
            c.execute(
                """UPDATE stage_runs SET ended_at = ?, result_status = ?,
                     cost_usd = COALESCE(?, cost_usd), num_turns = COALESCE(?, num_turns),
                     duration_ms = COALESCE(?, duration_ms),
                     session_id = COALESCE(?, session_id)
                   WHERE id = ? AND ended_at IS NULL""",
                (time.time(), result_status, cost_usd, num_turns, duration_ms, session_id, run_id),
            )

    def stage_run_mark_resumed(self, run_id: int):
        with self._conn() as c:
            c.execute("UPDATE stage_runs SET resumed = 1 WHERE id = ?", (run_id,))

    def stage_run_set_transcript(self, run_id: int, key: str):
        with self._conn() as c:
            c.execute("UPDATE stage_runs SET transcript = ? WHERE id = ?", (key, run_id))

    def stage_run_gate_posted(self, run_id: int):
        with self._conn() as c:
            c.execute("UPDATE stage_runs SET gate_posted_at = ? WHERE id = ?", (time.time(), run_id))

    def stage_run_gate_answered(self, job_id: str, stage: int, action: str):
        """Stamp the latest gated run of (job, stage) with the human's answer time."""
        with self._conn() as c:
            row = c.execute(
                "SELECT id FROM stage_runs WHERE job_id = ? AND stage = ? ORDER BY id DESC LIMIT 1",
                (job_id, stage),
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE stage_runs SET gate_answered_at = ?, gate_action = ? WHERE id = ?",
                    (time.time(), action, row["id"]),
                )

    def latest_gate_posted(self, job_id: str, stage: int) -> float:
        """When the current gate was posted — the SLA / inbox-age clock. Falls
        back to the job's updated_at (error/timeout parks may never have posted
        a gate; 'overdue since epoch' must be impossible)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(gate_posted_at) AS t FROM stage_runs "
                "WHERE job_id = ? AND stage = ? AND gate_posted_at IS NOT NULL",
                (job_id, stage),
            ).fetchone()
            if row and row["t"]:
                return row["t"]
            jrow = c.execute("SELECT updated_at FROM jobs WHERE issue_id = ?",
                             (job_id,)).fetchone()
            return (jrow["updated_at"] if jrow else 0) or 0

    def stage_runs_for(self, job_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM stage_runs WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- autonomy (Epic C: the trust ladder, docs/ENGINE.md §15) ----------
    # Scores/pins/events are configuration + telemetry, each written in a
    # single atomic statement — deliberately OUTSIDE the job-state CAS paths.

    def autonomy_run_rows(self, since: float) -> list[dict]:
        """The scorer's single stage_runs input: feature-job runs started in
        the window, stamped with their job's workspace/project. Stage 9 rows
        are excluded at the source (levels cover 0–8 only; P9 is terminal)."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT r.*, j.workspace_id AS workspace_id, j.project AS project
                   FROM stage_runs r JOIN jobs j ON j.issue_id = r.job_id
                   WHERE j.kind = 'feature' AND j.workspace_id IS NOT NULL
                     AND r.stage BETWEEN 0 AND 8 AND r.started_at >= ?
                   ORDER BY r.id""", (since,)).fetchall()
            return [dict(r) for r in rows]

    def autonomy_redo_rows(self, since: float) -> list[dict]:
        """Human redos in the window, attributed to their TARGET stage —
        guidance_log records the stage the human actually rejected (a
        retargeted '/redo P<k>' answered at a P<n> gate lands on stage k),
        while stage_runs.gate_action stamps the parked stage n. The scorer's
        redo numerator reads THIS, never gate_action (which stays the
        answered-gate denominator only)."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT g.stage AS stage, g.at AS at,
                          j.workspace_id AS workspace_id, j.project AS project
                   FROM guidance_log g JOIN jobs j ON j.issue_id = g.job_id
                   WHERE g.action = 'redo' AND j.kind = 'feature'
                     AND j.workspace_id IS NOT NULL AND g.stage IS NOT NULL
                     AND g.at >= ?
                   ORDER BY g.id""", (since,)).fetchall()
            return [dict(r) for r in rows]

    def shepherd_rounds_by_project(self, since: float) -> dict[str, float]:
        """Avg review rounds per project over feature-job PRs touched in the
        window. kind='feature' is explicit — memory-bootstrap and outcome PRs
        are tracked in prs too and must not poison the signal."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT j.project AS project, AVG(p.review_rounds) AS rounds
                   FROM prs p JOIN jobs j ON j.issue_id = p.job_id
                   WHERE j.kind = 'feature' AND p.updated_at >= ?
                   GROUP BY j.project""", (since,)).fetchall()
            return {r["project"]: float(r["rounds"] or 0) for r in rows}

    def autonomy_score_upsert(self, workspace_id: int, project: str, stage: int,
                              level: int, score: float, inputs_json: str,
                              sample_runs: int, computed_started: float) -> dict:
        """Conditional upsert of one cell. The previous-level read, the write
        and the post-write re-read share ONE transaction; the DO UPDATE is
        guarded so a clawback that landed after the compute pass started can
        never be overwritten (clawback_at itself is never touched here).
        Theoretical under today's single-threaded sync SQLite; load-bearing
        under multi-worker Postgres (Epic F2). Returns
        {prev_level, level (stored after), applied}."""
        now = time.time()
        with self._conn() as c:
            row = c.execute(
                "SELECT level FROM autonomy_scores WHERE workspace_id = ? AND project = ? AND stage = ?",
                (workspace_id, project, stage)).fetchone()
            prev = row["level"] if row else None
            cur = c.execute(
                """INSERT INTO autonomy_scores
                     (workspace_id, project, stage, level, score, inputs, sample_runs, computed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(workspace_id, project, stage) DO UPDATE SET
                     level = excluded.level, score = excluded.score,
                     inputs = excluded.inputs, sample_runs = excluded.sample_runs,
                     computed_at = excluded.computed_at
                   WHERE autonomy_scores.clawback_at IS NULL
                      OR autonomy_scores.clawback_at < ?""",
                (workspace_id, project, stage, level, score, inputs_json,
                 sample_runs, now, computed_started))
            applied = cur.rowcount == 1
            after = c.execute(
                "SELECT level FROM autonomy_scores WHERE workspace_id = ? AND project = ? AND stage = ?",
                (workspace_id, project, stage)).fetchone()
            return {"prev_level": prev, "level": after["level"] if after else level,
                    "applied": applied}

    def autonomy_score_get(self, workspace_id: int, project: str, stage: int) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM autonomy_scores WHERE workspace_id = ? AND project = ? AND stage = ?",
                (workspace_id, project, stage)).fetchone()
            return dict(row) if row else None

    def autonomy_scores_for(self, workspace_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM autonomy_scores WHERE workspace_id = ? ORDER BY project, stage",
                (workspace_id,)).fetchall()
            return [dict(r) for r in rows]

    def autonomy_scores_all(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM autonomy_scores ORDER BY workspace_id, project, stage").fetchall()
            return [dict(r) for r in rows]

    def autonomy_clawback(self, workspace_id: int, stage: int,
                          project: str | None) -> list[str]:
        """Drop cell(s) to level 0 and stamp clawback_at — re-earning starts
        from zero (the scorer ignores runs started before the stamp). A
        workspace-wide clawback (project=None) derives its slug list HERE, in
        the same transaction: every project that ever earned a score row in
        this workspace UNION the current repo slugs — a slug since removed
        from (or moved out of) the workspace keeps its stale cell clawable.
        Returns the affected project slugs."""
        now = time.time()
        with self._conn() as c:
            if project is None:
                slugs = {r["project"] for r in c.execute(
                    "SELECT DISTINCT project FROM autonomy_scores WHERE workspace_id = ?",
                    (workspace_id,)).fetchall()}
                slugs |= {r["slug"] for r in c.execute(
                    "SELECT slug FROM workspace_repos WHERE workspace_id = ?",
                    (workspace_id,)).fetchall()}
            else:
                slugs = {project}
            for p in sorted(slugs):
                c.execute(
                    """INSERT INTO autonomy_scores
                         (workspace_id, project, stage, level, score, inputs,
                          sample_runs, clawback_at, computed_at)
                       VALUES (?, ?, ?, 0, 0, '{}', 0, ?, ?)
                       ON CONFLICT(workspace_id, project, stage) DO UPDATE SET
                         level = 0, score = 0,
                         clawback_at = excluded.clawback_at,
                         computed_at = excluded.computed_at""",
                    (workspace_id, p, stage, now, now))
            return sorted(slugs)

    def autonomy_pin_set(self, workspace_id: int, stage: int, pin: str, set_by: str):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO autonomy_pins (workspace_id, stage, pin, set_by, set_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (workspace_id, stage, pin, set_by, time.time()))

    def autonomy_pin_clear(self, workspace_id: int, stage: int):
        with self._conn() as c:
            c.execute("DELETE FROM autonomy_pins WHERE workspace_id = ? AND stage = ?",
                      (workspace_id, stage))

    def autonomy_pins_for(self, workspace_id: int) -> dict[int, dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM autonomy_pins WHERE workspace_id = ? ORDER BY stage",
                             (workspace_id,)).fetchall()
            return {r["stage"]: dict(r) for r in rows}

    def autonomy_pins_all(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM autonomy_pins ORDER BY workspace_id, stage").fetchall()
            return [dict(r) for r in rows]

    def autonomy_event_add(self, kind: str, workspace_id: int | None = None,
                           project: str = "", stage: int | None = None,
                           job_id: str = "", detail: str = "", actor: str = ""):
        """INSERT-only audit substrate for every autonomy mutation (Epic E4
        folds/exports these later; nothing here waits for it)."""
        with self._conn() as c:
            c.execute(
                """INSERT INTO autonomy_events
                     (workspace_id, project, stage, job_id, kind, detail, actor, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (workspace_id, project, stage, job_id, kind, detail, actor, time.time()))

    def autonomy_events_recent(self, workspace_ids: list[int] | None,
                               limit: int = 50) -> list[dict]:
        """Newest-first event log. None = all workspaces (admins)."""
        with self._conn() as c:
            if workspace_ids is None:
                rows = c.execute(
                    "SELECT * FROM autonomy_events ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
            elif not workspace_ids:
                rows = []
            else:
                marks = ",".join("?" for _ in workspace_ids)
                rows = c.execute(
                    f"SELECT * FROM autonomy_events WHERE workspace_id IN ({marks}) "
                    f"ORDER BY id DESC LIMIT ?",
                    (*workspace_ids, limit)).fetchall()
            return [dict(r) for r in rows]

    # ---------- gate chat (INSERT-only transcript) ----------

    def chat_add(self, job_id: str, stage: int, attempt: int, role: str, text: str,
                 cost_usd: float | None = None, num_turns: int | None = None,
                 duration_ms: float | None = None, session_id: str = "",
                 degraded: bool = False, lane: str = "", author: str = "") -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO gate_chat (job_id, stage, attempt, role, text,
                     cost_usd, num_turns, duration_ms, session_id, degraded, lane, author, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, stage, attempt, role, text, cost_usd, num_turns,
                 duration_ms, session_id, int(degraded), lane, author, time.time()),
            )
            return cur.lastrowid

    def chat_last(self, job_id: str, stage: int, attempt: int | None = None) -> dict | None:
        """Latest turn for a stage — scoped to one ATTEMPT when given, so a
        pending question from a redone attempt never blocks the fresh gate."""
        with self._conn() as c:
            if attempt is None:
                row = c.execute(
                    "SELECT * FROM gate_chat WHERE job_id = ? AND stage = ? ORDER BY id DESC LIMIT 1",
                    (job_id, stage),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT * FROM gate_chat WHERE job_id = ? AND stage = ? AND attempt = ?"
                    " ORDER BY id DESC LIMIT 1",
                    (job_id, stage, attempt),
                ).fetchone()
            return dict(row) if row else None

    def chat_for(self, job_id: str, stage: int | None = None) -> list[dict]:
        with self._conn() as c:
            if stage is None:
                rows = c.execute(
                    "SELECT * FROM gate_chat WHERE job_id = ? ORDER BY id", (job_id,)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM gate_chat WHERE job_id = ? AND stage = ? ORDER BY id",
                    (job_id, stage),
                ).fetchall()
            return [dict(r) for r in rows]

    # ---------- pull requests (one work packet can open several) ----------

    def pr_add(self, job_id: str, url: str, state: str = "draft") -> bool:
        """Record a PR a run opened. Idempotent by URL (runs re-print PR_URL
        lines across stages); returns True only when the row is NEW — callers
        key one-time actions (mark-ready, first review trigger) off that."""
        url = (url or "").strip().rstrip("/")
        if not url:
            return False
        m = PR_URL_PARTS_RE.search(url)
        repo, number = (m.group(1), int(m.group(2))) if m else ("", None)
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO prs (job_id, url, repo, number, state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO NOTHING""",
                (job_id, url, repo, number, state, now, now),
            )
            return cur.rowcount == 1

    def pr_get(self, url: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM prs WHERE url = ?",
                            ((url or "").strip().rstrip("/"),)).fetchone()
            return dict(row) if row else None

    def prs_for(self, job_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM prs WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def pr_set(self, url: str, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(
                f"UPDATE prs SET {cols}, updated_at = ? WHERE url = ?",
                (*fields.values(), time.time(), url),
            )

    def prs_in_state(self, states: tuple[str, ...] | list[str]) -> list[dict]:
        """Shepherd work-list: every tracked PR in one of the given states."""
        marks = ",".join("?" for _ in states)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM prs WHERE state IN ({marks}) ORDER BY id", tuple(states)
            ).fetchall()
            return [dict(r) for r in rows]

    def chat_count(self, job_id: str, stage: int, attempt: int | None = None) -> int:
        """Human turns for a stage — per ATTEMPT when given: the turn budget is
        per gate (chat_max_turns_per_GATE), and a redo parks a NEW gate, so spent
        turns from a rejected attempt must not starve the fresh one."""
        with self._conn() as c:
            if attempt is None:
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM gate_chat WHERE job_id = ? AND stage = ? AND role = 'human'",
                    (job_id, stage),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM gate_chat"
                    " WHERE job_id = ? AND stage = ? AND attempt = ? AND role = 'human'",
                    (job_id, stage, attempt),
                ).fetchone()
            return row["n"]

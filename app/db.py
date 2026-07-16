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
    owner TEXT DEFAULT '',           -- feature owner (ClickUp user id or name)
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
    resumed INTEGER NOT NULL DEFAULT 0  -- 1 = this run continued a STAGE_ASK session
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

CREATE TABLE IF NOT EXISTS app_config (
    key TEXT PRIMARY KEY,            -- project-context override key (config.RUNTIME_CONTEXT_KEYS)
    value TEXT NOT NULL,             -- JSON-encoded value
    updated_at REAL NOT NULL
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
    },
    "stage_runs": {
        "session_id": "TEXT DEFAULT ''",
        "resumed": "INTEGER NOT NULL DEFAULT 0",
    },
    "gate_chat": {
        "cost_usd": "REAL",
        "num_turns": "INTEGER",
        "duration_ms": "REAL",
        "session_id": "TEXT DEFAULT ''",
        "degraded": "INTEGER NOT NULL DEFAULT 0",
        "lane": "TEXT NOT NULL DEFAULT ''",
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
        with self._conn() as c:
            c.execute(
                """INSERT INTO app_config (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value = excluded.value, updated_at = excluded.updated_at""",
                (key, json.dumps(value), time.time()),
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
                     via: str, artifact_sha: str = ""):
        with self._conn() as c:
            c.execute(
                "INSERT INTO guidance_log (job_id, stage, action, text, via, artifact_sha, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, stage, action, text, via, artifact_sha, time.time()),
            )

    def guidance_for(self, job_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM guidance_log WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
            return [dict(r) for r in rows]

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

    @staticmethod
    def _clear_pipeline_state(c, job_id: str):
        c.execute("DELETE FROM artifact_state WHERE job_id = ?", (job_id,))
        c.execute("DELETE FROM stage_state WHERE job_id = ?", (job_id,))
        c.execute("DELETE FROM guidance_log WHERE job_id = ?", (job_id,))

    def feature_intake(self, job_id: str, title: str, project: str, **fields):
        """Atomic feature (re-)intake: child-state clears + row upsert + pipeline
        reset in ONE transaction — a crash can never leave status='received' with a
        stale stage and no stage_state (which would strand the job on restart)."""
        now = time.time()
        with self._conn() as c:
            self._clear_pipeline_state(c, job_id)
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
            cols = ", ".join(f"{k} = ?" for k in fields)
            c.execute(
                f"UPDATE jobs SET {cols}, updated_at = ? WHERE issue_id = ?",
                (*fields.values(), now, job_id),
            )

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

    def stage_runs_for(self, job_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM stage_runs WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- gate chat (INSERT-only transcript) ----------

    def chat_add(self, job_id: str, stage: int, attempt: int, role: str, text: str,
                 cost_usd: float | None = None, num_turns: int | None = None,
                 duration_ms: float | None = None, session_id: str = "",
                 degraded: bool = False, lane: str = "") -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO gate_chat (job_id, stage, attempt, role, text,
                     cost_usd, num_turns, duration_ms, session_id, degraded, lane, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, stage, attempt, role, text, cost_usd, num_turns,
                 duration_ms, session_id, int(degraded), lane, time.time()),
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

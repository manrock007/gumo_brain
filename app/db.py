import sqlite3
import time
from contextlib import contextmanager

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
    result_status TEXT DEFAULT ''
);
"""

MIGRATIONS = {  # jobs columns added after v1 shipped -> DDL, for in-place upgrade
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
}


class JobStore:
    def __init__(self, path: str):
        self._path = path
        with self._conn() as c:
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
            for col, ddl in MIGRATIONS.items():
                if col not in cols:
                    c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")

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
            "subtask_id": "", "synced_hash": "", "flags": ""
        }
        merged = {**{k: current[k] for k in ("subtask_id", "synced_hash", "flags")}, **fields}
        with self._conn() as c:
            c.execute(
                """INSERT INTO artifact_state (job_id, artifact, subtask_id, synced_hash, flags)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(job_id, artifact) DO UPDATE SET
                     subtask_id = excluded.subtask_id,
                     synced_hash = excluded.synced_hash,
                     flags = excluded.flags""",
                (job_id, artifact, merged["subtask_id"], merged["synced_hash"], merged["flags"]),
            )

    def artifacts_for(self, job_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM artifact_state WHERE job_id = ? ORDER BY artifact", (job_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def artifacts_clear(self, job_id: str):
        with self._conn() as c:
            c.execute("DELETE FROM artifact_state WHERE job_id = ?", (job_id,))
            c.execute("DELETE FROM stage_state WHERE job_id = ?", (job_id,))

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
                        duration_ms: float | None = None):
        with self._conn() as c:
            c.execute(
                """UPDATE stage_runs SET ended_at = ?, result_status = ?,
                     cost_usd = COALESCE(?, cost_usd), num_turns = COALESCE(?, num_turns),
                     duration_ms = COALESCE(?, duration_ms)
                   WHERE id = ?""",
                (time.time(), result_status, cost_usd, num_turns, duration_ms, run_id),
            )

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

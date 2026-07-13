import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    issue_id TEXT PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    issue_url TEXT DEFAULT '',
    status TEXT NOT NULL,            -- received | queued | running | awaiting_input
                                     -- | pr_opened | no_fix | skipped | error | timeout
    phase INTEGER NOT NULL DEFAULT 1,
    forced INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'webhook',  -- webhook | sweep | manual
    score REAL,
    grade_reasons TEXT,
    analysis TEXT,                   -- phase-1 root-cause analysis (HITL flow)
    guidance TEXT,                   -- human /proceed guidance from ClickUp
    clickup_task_id TEXT,
    clickup_task_url TEXT,
    comment_marker TEXT DEFAULT '',  -- last ClickUp comment id we processed
    pr_url TEXT,
    detail TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


class JobStore:
    def __init__(self, path: str):
        self._path = path
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

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
               title: str = "", project: str = ""):
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO jobs (issue_id, project, title, status, source, forced, attempts, created_at, updated_at)
                   VALUES (?, ?, ?, 'received', ?, ?, 1, ?, ?)
                   ON CONFLICT(issue_id) DO UPDATE SET
                     status = 'received',
                     source = excluded.source,
                     forced = MAX(jobs.forced, excluded.forced),
                     attempts = jobs.attempts + 1,
                     updated_at = excluded.updated_at""",
                (issue_id, project, title, source, int(forced), now, now),
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

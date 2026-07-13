import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    issue_id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,            -- queued | running | pr_opened | no_fix | error | timeout
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
        midnight = time.time() - (time.time() % 86400)
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE created_at >= ? AND status != 'skipped'",
                (midnight,),
            ).fetchone()
            return row["n"]

    def upsert(self, issue_id: str, project: str, title: str, status: str):
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO jobs (issue_id, project, title, status, attempts, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(issue_id) DO UPDATE SET
                     status = excluded.status,
                     attempts = jobs.attempts + 1,
                     updated_at = excluded.updated_at""",
                (issue_id, project, title, status, now, now),
            )

    def set_status(self, issue_id: str, status: str, pr_url: str | None = None, detail: str | None = None):
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET status = ?, pr_url = COALESCE(?, pr_url), detail = COALESCE(?, detail), updated_at = ? WHERE issue_id = ?",
                (status, pr_url, detail, time.time(), issue_id),
            )

    def recent(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

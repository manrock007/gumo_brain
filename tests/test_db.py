import sqlite3

from app.db import JobStore

OLD_SCHEMA = """CREATE TABLE jobs (
    issue_id TEXT PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    issue_url TEXT DEFAULT '',
    status TEXT NOT NULL,
    phase INTEGER NOT NULL DEFAULT 1,
    forced INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'webhook',
    score REAL,
    grade_reasons TEXT,
    analysis TEXT,
    guidance TEXT,
    clickup_task_id TEXT,
    clickup_task_url TEXT,
    comment_marker TEXT DEFAULT '',
    pr_url TEXT,
    detail TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);"""


def test_migrates_pre_kind_database(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO jobs (issue_id, status, created_at, updated_at) VALUES ('1', 'pr_opened', 1, 1)"
    )
    conn.commit()
    conn.close()

    store = JobStore(path)
    row = store.get("1")
    assert row["kind"] == "sentry"
    assert row["request"] == "" or row["request"] is None
    assert row["question"] == "" or row["question"] is None


def test_insert_sets_kind_and_reintake_resets_hitl_state(store):
    store.insert("task-x", source="manual", forced=True, title="T", project="web", kind="task")
    store.set_fields("task-x", phase=2, analysis="old analysis", guidance="old", question="q?")

    store.insert("task-x", source="manual", forced=True, title="T", project="web", kind="task")
    row = store.get("task-x")
    assert row["kind"] == "task"
    assert row["phase"] == 1
    assert row["analysis"] is None
    assert row["guidance"] is None
    assert row["question"] == ""
    assert row["attempts"] == 2


def test_by_status_and_recent(store):
    store.insert("1", source="webhook")
    store.insert("2", source="webhook")
    store.set_status("1", "awaiting_input")
    assert [r["issue_id"] for r in store.by_status(["awaiting_input"])] == ["1"]
    assert {r["issue_id"] for r in store.recent()} == {"1", "2"}


OLD_PRE_BRANCH_SCHEMA = """CREATE TABLE jobs (
    issue_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'sentry',
    project TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    phase INTEGER NOT NULL DEFAULT 1,
    stage INTEGER NOT NULL DEFAULT 0,
    forced INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'webhook',
    analysis TEXT,
    question TEXT DEFAULT '',
    guidance TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);"""


class TestBranchBackfill:
    """Epic 0.2: the jobs.branch column is backfilled ONCE (when the ALTER
    first adds it) with the exact historical 'brain/…' branch each pre-upgrade
    job already pushed — including the deliberate double prefix for features
    (job ids already carry 'feat-')."""

    def _old_db(self, tmp_path):
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.executescript(OLD_PRE_BRANCH_SCHEMA)
        rows = [
            ("feat-abc", "feature", "web", "awaiting_input"),
            ("task-xyz", "task", "web", "queued"),
            ("6613584091", "sentry", "web", "pr_opened"),
            ("mem-web", "memory", "web", "no_fix"),
        ]
        for issue_id, kind, project, status in rows:
            conn.execute(
                "INSERT INTO jobs (issue_id, kind, project, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 1, 1)", (issue_id, kind, project, status))
        conn.commit()
        conn.close()
        return path

    def test_backfill_assigns_historical_branches(self, tmp_path):
        store = JobStore(self._old_db(tmp_path))
        assert store.get("feat-abc")["branch"] == "brain/feat-feat-abc"
        assert store.get("task-xyz")["branch"] == "brain/task-xyz"
        assert store.get("6613584091")["branch"] == "brain/sentry-6613584091"
        assert store.get("mem-web")["branch"] == "brain/memory-web"

    def test_fresh_insert_has_empty_branch(self, tmp_path):
        store = JobStore(self._old_db(tmp_path))
        store.insert("task-new", source="manual", kind="task", project="web")
        assert store.get("task-new")["branch"] == ""

    def test_backfill_runs_once(self, tmp_path):
        path = self._old_db(tmp_path)
        store = JobStore(path)
        # a later boot must NOT re-backfill rows that legitimately have '' —
        # the column already exists, so the ALTER arm never fires again
        store.insert("task-later", source="manual", kind="task", project="web")
        store2 = JobStore(path)
        assert store2.get("task-later")["branch"] == ""

    def test_feature_reintake_keeps_the_stored_branch(self, store):
        """A fresh restart of a terminal skipped/no_fix pipeline reuses the
        SAME branch (origin continuity — mirrors the historical constant
        naming); feature_intake never touches jobs.branch."""
        store.feature_intake("feat-r1", title="F", project="web", stage=0)
        store.set_fields("feat-r1", branch="brain/feat-feat-r1")
        store.set_status("feat-r1", "skipped")
        store.feature_intake("feat-r1", title="F", project="web", stage=0)
        assert store.get("feat-r1")["branch"] == "brain/feat-feat-r1"

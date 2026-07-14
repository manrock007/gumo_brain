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

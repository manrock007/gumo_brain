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


class TestEpicAMigration:
    """Epic A additive columns land on a pre-existing DB file."""

    def test_new_columns_exist_after_migration(self, tmp_path):
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.executescript(OLD_PRE_BRANCH_SCHEMA)
        conn.execute(
            "INSERT INTO jobs (issue_id, kind, project, status, created_at, updated_at)"
            " VALUES ('feat-m1', 'feature', 'web', 'awaiting_input', 1, 1)")
        conn.commit()
        conn.close()

        store = JobStore(path)
        row = store.get("feat-m1")
        assert row["founder_dri"] == "" or row["founder_dri"] is None
        assert row["dev_dri"] == "" or row["dev_dri"] is None
        # users + workspaces gained their columns too (INSERT proves them)
        store.user_create("u1", "hash")
        assert store.user_get("u1")["clickup_user_id"] == ""
        ws = store.workspace_create("w1", "W1")
        assert ws["require_attributed_answers"] == "auto"
        assert ws["stage_role_map"] == ""
        assert ws["gate_sla_hours"] is None

    def test_gate_event_dedupe_and_empty_ref(self, store):
        assert store.gate_event_add("j1", "sla_nudge", ref="run1-step1", stage=3) is True
        assert store.gate_event_add("j1", "sla_nudge", ref="run1-step1", stage=3) is False
        # a different kind or ref is a fresh event
        assert store.gate_event_add("j1", "sla_second_dri", ref="run1-step2") is True
        assert store.gate_event_add("j2", "sla_nudge", ref="run1-step1") is True
        events = store.gate_events_for("j1")
        assert [e["kind"] for e in events] == ["sla_nudge", "sla_second_dri"]
        # an empty ref would silently dedupe unrelated events — refused loudly
        import pytest
        with pytest.raises(ValueError):
            store.gate_event_add("j1", "sla_nudge", ref="")

    def test_clickup_id_unique_index_blocks_duplicates(self, store):
        import pytest

        store.user_create("a1", "hash")
        store.user_set("a1", clickup_user_id="777")
        assert store.user_for_clickup_id("777")["username"] == "a1"
        store.user_create("a2", "hash")
        with pytest.raises(sqlite3.IntegrityError):
            store.user_set("a2", clickup_user_id="777")
        # empty mappings are exempt from uniqueness (partial index)
        store.user_create("a3", "hash")
        store.user_set("a3", clickup_user_id="")

    def test_user_for_clickup_id_duplicate_rows(self, tmp_path):
        """Belt-and-braces: a hand-edited DB with two users on one ClickUp id
        must resolve to None (ambiguity fails closed), never pick one."""
        store = JobStore(str(tmp_path / "dup.db"))
        store.user_create("b1", "hash")
        store.user_create("b2", "hash")
        with store._conn() as c:  # bypass the unique index the way a hand edit would
            c.execute("DROP INDEX idx_users_clickup_id")
            c.execute("UPDATE users SET clickup_user_id = '888'")
        assert store.user_for_clickup_id("888") is None

    def test_user_for_dri_numeric_and_username(self, store):
        store.user_create("dev1", "hash")
        store.user_set("dev1", clickup_user_id="4242")
        assert store.user_for_dri("4242")["username"] == "dev1"
        assert store.user_for_dri("dev1")["username"] == "dev1"
        # a digits-only USERNAME is still reachable when no mapping matches
        store.user_create("12345", "hash")
        assert store.user_for_dri("12345")["username"] == "12345"
        assert store.user_for_dri("") is None
        # disabled users never resolve through the mapping
        store.user_set("dev1", disabled=1)
        assert store.user_for_dri("4242") is None

    def test_latest_gate_posted_falls_back_to_updated_at(self, store):
        store.feature_intake("feat-g1", title="F", project="web", stage=5)
        row = store.get("feat-g1")
        # no stage_runs at all -> updated_at, never 0 ("overdue since epoch")
        assert store.latest_gate_posted("feat-g1", 5) == row["updated_at"]
        rid = store.stage_run_open("feat-g1", 5, 1)
        store.stage_run_gate_posted(rid)
        runs = store.stage_runs_for("feat-g1")
        assert store.latest_gate_posted("feat-g1", 5) == runs[-1]["gate_posted_at"]
        # unknown job -> 0
        assert store.latest_gate_posted("nope", 1) == 0

    def test_awaiting_gates_includes_feature_error(self, store):
        store.feature_intake("feat-a1", title="F", project="web", stage=2)
        store.set_status("feat-a1", "awaiting_input")
        store.feature_intake("feat-a2", title="F", project="web", stage=3)
        store.set_status("feat-a2", "error")
        store.insert("task-a1", source="manual", kind="task", project="web")
        store.set_status("task-a1", "error")  # v1 error is NOT answerable
        ids = {r["issue_id"] for r in store.awaiting_gates()}
        assert ids == {"feat-a1", "feat-a2"}

import asyncio
import subprocess
from pathlib import Path

import pytest

from app.artifacts import (
    ArtifactSync,
    artifact_path,
    list_artifacts,
    normalize,
    semantic_hash,
    semantic_normalize,
)

JOB = "feat-t1"


class TestNormalization:
    def test_normalize_line_endings_and_trailing_ws(self):
        assert normalize("a  \r\nb\r") == "a\nb\n"

    def test_semantic_strips_clickup_escapes(self):
        assert semantic_normalize("a \\- b \\_c\\_") == semantic_normalize("a - b _c_")

    def test_semantic_unifies_bullets(self):
        assert semantic_normalize("* one\n+ two\n- three") == semantic_normalize("- one\n- two\n- three")

    def test_semantic_unifies_ordered_numbering(self):
        assert semantic_normalize("1. a\n2. b\n3) c") == semantic_normalize("1. a\n1. b\n1. c")

    def test_semantic_collapses_blank_runs(self):
        assert semantic_normalize("a\n\n\n\n\nb") == semantic_normalize("a\n\nb")

    def test_semantic_table_whitespace(self):
        assert semantic_normalize("| a  |  b |") == semantic_normalize("| a | b |")

    def test_real_edit_still_differs(self):
        assert semantic_hash("scope: exports only") != semantic_hash("scope: exports and imports")

    def test_mangled_roundtrip_is_same_document(self):
        original = "## Plan\n\n- step one\n- step_two\n\n1. first\n2. second"
        mangled = "## Plan\n\n* step one\n* step\\_two\n\n\n1. first\n1. second"
        assert semantic_hash(original) == semantic_hash(mangled)


def _mangle(text: str) -> str:
    """Simulate ClickUp's markdown regeneration."""
    return text.replace("- ", "* ").replace("_", "\\_")


class FakeClickUp:
    enabled = True

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.comments_posted: list[str] = []
        self._n = 0

    async def get_task(self, task_id):
        t = self.tasks.get(task_id)
        if t is None:
            return {"missing": True, "id": task_id}
        return {"id": task_id, "name": t.get("name", ""), "url": "http://cu/" + task_id,
                "list_id": "L1", "archived": t.get("archived", False),
                "description": _mangle(t["description"])}

    async def create_task(self, name, description, list_id=None, parent=None):
        if self.tasks.get("__fail_create__"):
            return None
        self._n += 1
        tid = f"st{self._n}"
        self.tasks[tid] = {"name": name, "description": description, "list_id": list_id, "parent": parent}
        return tid, "http://cu/" + tid

    async def update_description(self, task_id, markdown):
        if task_id not in self.tasks:
            return False
        self.tasks[task_id]["description"] = markdown
        return True

    async def comment(self, task_id, text):
        self.comments_posted.append(text)

    async def set_assignee(self, task_id, user_id):
        return True


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit",
                    "--allow-empty", "-q", "-m", "init"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=ws, check=True)
    return str(ws)


@pytest.fixture()
def sync(store):
    return ArtifactSync(store, FakeClickUp(), mirror_max_chars=5000)


@pytest.fixture()
def job(store):
    store.insert(JOB, source="manual", forced=True, title="F", project="web", kind="feature")
    store.set_fields(JOB, clickup_task_id="parent1", cu_list_id="L1", mirror_ok=1)
    return store.get(JOB)


def _write_artifact(workspace, name, content):
    p = artifact_path(workspace, JOB, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


class TestPushPull:
    def test_push_creates_mirror_and_stores_readback_hash(self, sync, job, workspace, store):
        sync.clickup.tasks["parent1"] = {"description": "parent"}
        _write_artifact(workspace, "P1-prd.md", "## PRD\n\n- item_one\n")
        asyncio.run(sync.push(workspace, job))

        state = store.artifact_get(JOB, "P1-prd.md")
        assert state and state["subtask_id"]
        # the stored hash is of the MANGLED readback (ClickUp's fixpoint)...
        mirror = sync.clickup.tasks[state["subtask_id"]]["description"]
        assert state["synced_hash"] == semantic_hash(_mangle(mirror))
        # ...which is semantically identical to the git file
        assert semantic_hash(mirror) == semantic_hash("## PRD\n\n- item_one\n")

    def test_pull_ignores_roundtrip_mangling(self, sync, job, workspace, store):
        sync.clickup.tasks["parent1"] = {"description": "parent"}
        _write_artifact(workspace, "P1-prd.md", "## PRD\n\n- item_one\n")
        asyncio.run(sync.push(workspace, job))

        edited = asyncio.run(sync.pull(workspace, job))
        assert edited == []
        # file untouched
        assert artifact_path(workspace, JOB, "P1-prd.md").read_text() == "## PRD\n\n- item_one\n"

    def test_pull_folds_in_human_edit(self, sync, job, workspace, store):
        sync.clickup.tasks["parent1"] = {"description": "parent"}
        _write_artifact(workspace, "P1-prd.md", "## PRD\n\nscope: exports\n")
        asyncio.run(sync.push(workspace, job))

        st = store.artifact_get(JOB, "P1-prd.md")["subtask_id"]
        sync.clickup.tasks[st]["description"] = "## PRD\n\nscope: exports AND imports\n"
        edited = asyncio.run(sync.pull(workspace, job))

        assert edited == ["P1-prd.md"]
        content = artifact_path(workspace, JOB, "P1-prd.md").read_text()
        assert "AND imports" in content
        # committed to git
        log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=workspace,
                             capture_output=True, text=True).stdout
        assert "human edit" in log


class TestContentCache:
    """docs/CONVERSATIONS.md §5: the fast-lane chat bundle reads artifact
    bodies from the DB cache — refreshed on every write path, including when
    ClickUp mirroring is off entirely."""

    def test_push_caches_content_even_with_clickup_disabled(self, store, job, workspace):
        clickup = FakeClickUp()
        clickup.enabled = False
        sync = ArtifactSync(store, clickup, mirror_max_chars=5000)
        _write_artifact(workspace, "P3-design.md", "## Design\n\nuse model B\n")
        asyncio.run(sync.push(workspace, job))
        cached = store.artifact_contents(JOB, ["P3-design.md"])
        assert "use model B" in cached["P3-design.md"]

    def test_commit_file_caches_content(self, sync, job, workspace, store):
        asyncio.run(sync.commit_file(workspace, JOB, "P1-prd.md",
                                     "## PRD\n\nscope: exports\n", "P1: artifact"))
        cached = store.artifact_contents(JOB, ["P1-prd.md"])
        assert "scope: exports" in cached["P1-prd.md"]

    def test_pulled_human_edit_refreshes_cache(self, sync, job, workspace, store):
        sync.clickup.tasks["parent1"] = {"description": "parent"}
        _write_artifact(workspace, "P1-prd.md", "## PRD\n\nscope: exports\n")
        asyncio.run(sync.push(workspace, job))
        st = store.artifact_get(JOB, "P1-prd.md")["subtask_id"]
        sync.clickup.tasks[st]["description"] = "## PRD\n\nscope: exports AND imports\n"
        asyncio.run(sync.pull(workspace, job))
        cached = store.artifact_contents(JOB, ["P1-prd.md"])
        assert "AND imports" in cached["P1-prd.md"]

    def test_cache_truncates_and_stats_stay_lean(self, store, job, workspace):
        from app.db import ARTIFACT_CONTENT_MAX

        store.artifact_content_set(JOB, "P4-plan.md", "x" * (ARTIFACT_CONTENT_MAX + 500))
        cached = store.artifact_contents(JOB, ["P4-plan.md"])["P4-plan.md"]
        assert len(cached) <= ARTIFACT_CONTENT_MAX + 30
        assert cached.endswith("(truncated cache)")
        # the stats/sync view must NOT ship bodies over the API
        rows = store.artifacts_for(JOB)
        assert all("content" not in r for r in rows)

    def test_content_survives_sync_hash_updates(self, store, job):
        store.artifact_content_set(JOB, "P1-prd.md", "body text")
        store.artifact_set(JOB, "P1-prd.md", synced_hash="h1")  # sync bookkeeping
        assert store.artifact_contents(JOB, ["P1-prd.md"])["P1-prd.md"] == "body text"

    def test_pull_empty_description_never_wins(self, sync, job, workspace, store):
        sync.clickup.tasks["parent1"] = {"description": "parent"}
        _write_artifact(workspace, "P1-prd.md", "## PRD\n\ncontent\n")
        asyncio.run(sync.push(workspace, job))

        st = store.artifact_get(JOB, "P1-prd.md")["subtask_id"]
        sync.clickup.tasks[st]["description"] = ""
        edited = asyncio.run(sync.pull(workspace, job))
        assert edited == []
        assert "content" in artifact_path(workspace, JOB, "P1-prd.md").read_text()

    def test_pull_recreates_deleted_mirror_from_git(self, sync, job, workspace, store):
        sync.clickup.tasks["parent1"] = {"description": "parent"}
        _write_artifact(workspace, "P1-prd.md", "## PRD\n\ncontent\n")
        asyncio.run(sync.push(workspace, job))

        old = store.artifact_get(JOB, "P1-prd.md")["subtask_id"]
        del sync.clickup.tasks[old]
        asyncio.run(sync.pull(workspace, job))

        state = store.artifact_get(JOB, "P1-prd.md")
        assert state["subtask_id"] and state["subtask_id"] != old
        assert "content" in sync.clickup.tasks[state["subtask_id"]]["description"]

    def test_push_human_edit_during_run_wins(self, sync, job, workspace, store):
        sync.clickup.tasks["parent1"] = {"description": "parent"}
        _write_artifact(workspace, "P4-plan.md", "## Plan\n\n- original step\n")
        asyncio.run(sync.push(workspace, job))
        st = store.artifact_get(JOB, "P4-plan.md")["subtask_id"]

        # human edits the mirror while the stage rewrites the file
        sync.clickup.tasks[st]["description"] = "## Plan\n\n- HUMAN reordered step\n"
        _write_artifact(workspace, "P4-plan.md", "## Plan\n\n- claude changed step\n")
        conflicted = asyncio.run(sync.push(workspace, job))

        assert conflicted == ["P4-plan.md"]
        # human version is now the git truth; mirror untouched by the engine
        assert "HUMAN" in artifact_path(workspace, JOB, "P4-plan.md").read_text()
        assert "HUMAN" in sync.clickup.tasks[st]["description"]

    def test_oversize_artifact_becomes_pointer_mirror(self, sync, job, workspace, store):
        sync.clickup.tasks["parent1"] = {"description": "parent"}
        _write_artifact(workspace, "P3-design.md", "x" * 6000)
        asyncio.run(sync.push(workspace, job))

        state = store.artifact_get(JOB, "P3-design.md")
        assert "truncated" in state["flags"]
        mirror = sync.clickup.tasks[state["subtask_id"]]["description"]
        assert "TRUNCATED MIRROR" in mirror
        # truncated mirrors are excluded from edit pulls
        st = state["subtask_id"]
        sync.clickup.tasks[st]["description"] = "human scribbles on pointer"
        assert asyncio.run(sync.pull(workspace, job)) == []

    def test_truncated_mirror_never_committed_back_as_human_edit(self, sync, job, workspace, store):
        # regression: the pointer banner must not be mistaken for a human edit on
        # the next push() and written into git over the real (large) artifact.
        sync.clickup.tasks["parent1"] = {"description": "parent"}
        big = "line of design text\n" * 400  # > 5000 chars
        _write_artifact(workspace, "P3-design.md", big)
        asyncio.run(sync.push(workspace, job))
        assert "truncated" in store.artifact_get(JOB, "P3-design.md")["flags"]

        # a second stage re-pushes the same artifact; must NOT clobber git with the banner
        conflicted = asyncio.run(sync.push(workspace, job))
        assert conflicted == []
        assert "TRUNCATED MIRROR" not in artifact_path(workspace, JOB, "P3-design.md").read_text()
        assert artifact_path(workspace, JOB, "P3-design.md").read_text().startswith("line of design text")

    def test_mirror_create_failure_flags_job_not_silent(self, sync, job, workspace, store):
        sync.clickup.tasks["parent1"] = {"description": "parent"}
        sync.clickup.tasks["__fail_create__"] = True
        _write_artifact(workspace, "P1-prd.md", "## PRD\n")
        asyncio.run(sync.push(workspace, job))

        assert store.get(JOB)["mirror_ok"] == 0
        assert any("mirror" in c.lower() for c in sync.clickup.comments_posted)

    def test_commit_file_idempotent_under_replay(self, sync, job, workspace):
        wrote1 = asyncio.run(sync.commit_file(workspace, JOB, "P0-intake.md", "same\n", "m"))
        wrote2 = asyncio.run(sync.commit_file(workspace, JOB, "P0-intake.md", "same\n", "m"))
        assert wrote1 is True and wrote2 is False

    def test_list_artifacts_sorted(self, workspace):
        _write_artifact(workspace, "P3-design.md", "x")
        _write_artifact(workspace, "P0-intake.md", "x")
        assert list_artifacts(workspace, JOB) == ["P0-intake.md", "P3-design.md"]

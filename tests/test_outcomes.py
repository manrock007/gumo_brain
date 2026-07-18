"""The outcome ledger (Epic B5): workspace-scoped API, the memory entries, and
the mechanical draft-PR propagation task."""

import asyncio
import base64
import importlib
import json

import pytest
from fastapi.testclient import TestClient

from app.outcome import build_outcome_adr, build_outcome_entry


AUTH = {"Authorization": "Basic " + base64.b64encode(b"gumo:test").decode()}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test")

    from app import config

    config.get_settings.cache_clear()
    from app import main as main_module

    importlib.reload(main_module)
    with TestClient(main_module.app) as c:
        yield c
    config.get_settings.cache_clear()


class TestOutcomesScoping:
    def test_member_never_sees_other_workspaces_rows(self, client):
        store = client.app.state.store
        default_ws = client.get("/api/workspaces", headers=AUTH).json()[0]
        other = client.post("/api/workspaces", headers=AUTH, json={
            "slug": "other", "name": "Other"}).json()
        store.outcome_add("watch-feat-a", "feat-a", default_ws["id"], verdict="moved")
        store.outcome_add("watch-feat-b", "feat-b", other["id"], verdict="regressed")
        store.outcome_add("watch-feat-c", "feat-c", None, verdict="flat")  # unowned

        client.post("/api/users", headers=AUTH, json={
            "username": "omem", "password": "password1"})
        client.put(f"/api/workspaces/{default_ws['id']}/members", headers=AUTH,
                   json={"username": "omem", "member": True})
        mem = {"Authorization": "Basic " + base64.b64encode(b"omem:password1").decode()}

        data = client.get("/api/outcomes", headers=mem).json()
        # fail-closed filtering: only the assigned workspace's rows exist here —
        # unowned rows (workspace NULL) stay admin-only
        assert [o["feature_id"] for o in data["outcomes"]] == ["feat-a"]
        assert data["verdicts"] == {"moved": 1, "flat": 0, "regressed": 0,
                                    "unmeasured": 0}
        # the admin sees everything, distribution included
        data = client.get("/api/outcomes", headers=AUTH).json()
        assert {o["feature_id"] for o in data["outcomes"]} == {"feat-a", "feat-b", "feat-c"}
        assert data["verdicts"]["regressed"] == 1


class TestOutcomeMemoryContent:
    OUTCOME = {"job_id": "watch-feat-x", "feature_id": "feat-x", "metric": "signups",
               "metric_event": "signup_done", "target": ">= 100", "observed": 120.0,
               "baseline": 80.0, "window_days": 14, "verdict": "moved",
               "learning": "smaller batches win", "decided_by": "dashboard:manish"}
    FEATURE = {"issue_id": "feat-x", "title": "CSV export",
               "pr_url": "https://github.com/o/r/pull/9",
               "clickup_task_url": "https://cu/t1"}

    def test_changelog_entry_content_and_namespaced_path(self):
        path, body = build_outcome_entry(self.OUTCOME, self.FEATURE, ns=".gumo")
        assert path.startswith(".gumo/memory/changelog/")
        assert path.endswith("-outcome-feat-x.md")
        assert "CSV export" in body and "moved" in body
        assert "signups" in body and "`signup_done`" in body
        assert ">= 100" in body and "120.0" in body
        assert "https://github.com/o/r/pull/9" in body
        assert "smaller batches win" in body
        assert "dashboard:manish" in body

    def test_default_namespace_is_the_engine_constant(self):
        path, _ = build_outcome_entry(self.OUTCOME, self.FEATURE)
        assert path.startswith(".ctrlloop/memory/changelog/")

    def test_adr_content(self):
        path, body = build_outcome_adr(self.OUTCOME, self.FEATURE, ns=".ctrlloop")
        assert path.startswith(".ctrlloop/memory/decisions/")
        assert "smaller batches win" in body
        assert "verdict" in body and "moved" in body


class TestOutcomeMemoryTask:
    def _watch(self, worker, job_id="watch-feat-m1"):
        # feature FIRST: its (re-)intake clears watch-<id> state by design
        feature_id = job_id.removeprefix("watch-")
        worker.store.feature_intake(feature_id, title="F", project="web", stage=9)
        worker.store.watch_insert(job_id, title="watch: F", project="web",
                                  related_jobs=feature_id, clickup_task_id="cu1")
        worker.store.outcome_add(job_id, feature_id, None, metric="m",
                                 verdict="moved", observed=5.0)
        worker.store.outcome_set(job_id, learning="a learning")
        return worker.store.get(job_id)

    def test_writes_files_and_records_a_draft_pr(self, worker, monkeypatch, tmp_path):
        import app.worker as worker_mod

        job = self._watch(worker)
        gits = []

        async def fake_ws(settings, target, branch, **kw):
            return str(tmp_path)

        async def fake_git(ws, *args, **kw):
            gits.append(args)
            return (0, "")

        created = {}

        async def fake_create_pr(repo, head, base, title, body, draft=True):
            created.update(repo=repo, head=head, base=base, draft=draft)
            return "https://github.com/acme/web/pull/77"

        monkeypatch.setattr(worker_mod, "prepare_workspace", fake_ws)
        monkeypatch.setattr(worker_mod, "git", fake_git)
        monkeypatch.setattr(worker.engine.github, "create_pr", fake_create_pr)
        asyncio.run(worker._outcome_memory_task(job))

        entries = list((tmp_path / ".ctrlloop" / "memory" / "changelog").glob("*.md"))
        adrs = list((tmp_path / ".ctrlloop" / "memory" / "decisions").glob("*.md"))
        assert len(entries) == 1 and "outcome-feat-m1" in entries[0].name
        assert len(adrs) == 1  # learning non-empty -> ADR written
        assert "a learning" in adrs[0].read_text()
        assert any(a[0] == "push" for a in gits)
        assert created["draft"] is True
        assert created["head"] == "ctrlloop/outcome-feat-m1"
        # tracked as a doc draft (kickoff=False): recorded, still 'draft'
        prs = worker.store.prs_for("watch-feat-m1")
        assert [p["state"] for p in prs] == ["draft"]

    def test_no_adr_without_a_learning(self, worker, monkeypatch, tmp_path):
        import app.worker as worker_mod

        job = self._watch(worker, "watch-feat-m2")
        worker.store.outcome_set("watch-feat-m2", learning="")

        async def fake_ws(settings, target, branch, **kw):
            return str(tmp_path)

        async def fake_git(ws, *args, **kw):
            return (0, "")

        async def fake_create_pr(*a, **k):
            return "https://github.com/acme/web/pull/78"

        monkeypatch.setattr(worker_mod, "prepare_workspace", fake_ws)
        monkeypatch.setattr(worker_mod, "git", fake_git)
        monkeypatch.setattr(worker.engine.github, "create_pr", fake_create_pr)
        asyncio.run(worker._outcome_memory_task(job))
        assert list((tmp_path / ".ctrlloop" / "memory" / "changelog").glob("*.md"))
        assert not (tmp_path / ".ctrlloop" / "memory" / "decisions").exists()

    def test_failure_sets_detail_and_never_raises(self, worker, monkeypatch, tmp_path):
        import app.worker as worker_mod

        job = self._watch(worker, "watch-feat-m3")

        async def fake_ws(settings, target, branch, **kw):
            return str(tmp_path)

        async def failing_git(ws, *args, **kw):
            if args and args[0] == "push":
                return (1, "remote rejected")
            return (0, "")

        monkeypatch.setattr(worker_mod, "prepare_workspace", fake_ws)
        monkeypatch.setattr(worker_mod, "git", failing_git)
        asyncio.run(worker._outcome_memory_task(job))  # must not raise
        assert "failed" in worker.store.get("watch-feat-m3")["detail"]
        assert worker.store.prs_for("watch-feat-m3") == []

    def test_unmapped_project_skips_cleanly(self, worker):
        job = self._watch(worker, "watch-feat-m4")
        worker.store.set_fields("watch-feat-m4", project="nope")
        asyncio.run(worker._outcome_memory_task(worker.store.get("watch-feat-m4")))
        assert "no repo mapped" in worker.store.get("watch-feat-m4")["detail"]

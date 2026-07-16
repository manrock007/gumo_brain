"""Memory jobs and the reaper must speak on the owning ClickUp ticket —
dogfood-found: a 2h-silent memory bootstrap was externally indistinguishable
from a stuck one, because neither success, failure, nor a reap ever posted."""

import asyncio
import time


class FakeClickUp:
    enabled = True

    def __init__(self):
        self.comments_posted = []

    async def comment(self, task_id, text):
        self.comments_posted.append((task_id, text))

    async def set_status(self, task_id, state):
        pass


class TestMemoryBootstrapVisibility:
    def _memory_job(self, worker, project="web"):
        worker.intake_memory(project)
        worker.store.set_fields(f"mem-{project}", clickup_task_id="cu-mem",
                                clickup_task_url="https://cu/x")
        return worker.store.get(f"mem-{project}")

    def test_success_comments_the_pr_link(self, worker, monkeypatch, tmp_path):
        import app.engine as engine_mod
        from app.fixer import RawRunResult

        fake = FakeClickUp()
        worker.engine.clickup = fake
        job = self._memory_job(worker)

        async def fake_ws(*a, **k):
            return str(tmp_path)

        async def fake_invoke(*a, **k):
            return RawRunResult(
                "ok", "STAGE_DONE: docs drafted\nPR_URL: https://github.com/o/r/pull/77", {})

        async def fake_checkpoint(*a, **k):
            return True

        async def fake_refresh(*a, **k):
            return None

        monkeypatch.setattr(engine_mod, "prepare_workspace", fake_ws)
        monkeypatch.setattr(engine_mod, "prepare_feature_workspace", fake_ws)
        monkeypatch.setattr(worker.engine, "_invoke", fake_invoke)
        monkeypatch.setattr(worker.engine, "_checkpoint", fake_checkpoint)
        monkeypatch.setattr(worker.engine.memory, "refresh_cache", fake_refresh)
        asyncio.run(worker.engine.run_memory_bootstrap(job))

        assert worker.store.get("mem-web")["status"] == "pr_opened"
        assert any("pull/77" in t for _, t in fake.comments_posted)

    def test_error_comments_with_retry_hint(self, worker, monkeypatch, tmp_path):
        import app.engine as engine_mod
        from app.fixer import RawRunResult

        fake = FakeClickUp()
        worker.engine.clickup = fake
        job = self._memory_job(worker, "react-native")

        async def fake_ws(*a, **k):
            return str(tmp_path)

        async def fake_invoke(*a, **k):
            return RawRunResult("error", "API Error: Server error", {})

        async def fake_checkpoint(*a, **k):
            return True

        monkeypatch.setattr(engine_mod, "prepare_workspace", fake_ws)
        monkeypatch.setattr(engine_mod, "prepare_feature_workspace", fake_ws)
        monkeypatch.setattr(worker.engine, "_invoke", fake_invoke)
        monkeypatch.setattr(worker.engine, "_checkpoint", fake_checkpoint)
        asyncio.run(worker.engine.run_memory_bootstrap(job))

        assert worker.store.get("mem-react-native")["status"] == "error"
        assert any("re-file" in t for _, t in fake.comments_posted)


class TestReaperVisibility:
    def test_reap_comments_the_ticket(self, worker):
        fake = FakeClickUp()
        worker.clickup = fake
        worker.intake_task("task-rp1", title="T", project="web", request="r",
                           clickup_task_id="cu-rp1")
        worker.store.set_fields("task-rp1", run_started_at=time.time() - 99999)
        worker.store.set_status("task-rp1", "running")

        asyncio.run(worker._reap_once(horizon=3600))
        assert worker.store.get("task-rp1")["status"] == "error"
        assert any("reaped" in t for tid, t in fake.comments_posted if tid == "cu-rp1")

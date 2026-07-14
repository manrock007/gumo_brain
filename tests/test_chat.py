import asyncio
import base64
import time

import pytest
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Basic " + base64.b64encode(b"gumo:test").decode()}


class TestChatStore:
    def test_chat_add_and_transcript(self, store):
        store.insert("feat-c1", source="manual", forced=True, project="web", kind="feature")
        store.chat_add("feat-c1", 3, 1, "human", "why option B?")
        store.chat_add("feat-c1", 3, 1, "engine", "because ...", cost_usd=0.04,
                       duration_ms=12000, session_id="s1")
        turns = store.chat_for("feat-c1", 3)
        assert [t["role"] for t in turns] == ["human", "engine"]
        assert turns[1]["cost_usd"] == 0.04
        assert store.chat_count("feat-c1", 3) == 1  # human turns only
        assert store.chat_last("feat-c1", 3)["role"] == "engine"

    def test_chat_scoped_by_stage(self, store):
        store.insert("feat-c2", source="manual", forced=True, project="web", kind="feature")
        store.chat_add("feat-c2", 1, 1, "human", "q1")
        store.chat_add("feat-c2", 2, 1, "human", "q2")
        assert len(store.chat_for("feat-c2", 1)) == 1
        assert len(store.chat_for("feat-c2")) == 2


class TestChatEngineFallbacks:
    def test_unknown_repo_degrades(self, worker):
        worker.intake_feature("feat-c3", title="F", project="web", request="r")
        worker.store.set_fields("feat-c3", project="not-mapped", stage=2, stage_attempts=1)
        worker.store.set_status("feat-c3", "awaiting_input")
        job = worker.store.get("feat-c3")
        worker.store.chat_add("feat-c3", 2, 1, "human", "q?")
        asyncio.run(worker.engine.chat(job, "q?"))
        last = worker.store.chat_last("feat-c3", 2)
        assert last["role"] == "engine"
        assert last["degraded"] == 1

    def test_gate_answered_before_reply(self, worker):
        worker.intake_feature("feat-c4", title="F", project="web", request="r")
        worker.store.set_fields("feat-c4", stage=2, stage_attempts=1)
        worker.store.set_status("feat-c4", "queued")  # no longer parked
        job = dict(worker.store.get("feat-c4"))
        job["status"] = "awaiting_input"  # stale snapshot, as the endpoint saw it
        worker.store.chat_add("feat-c4", 2, 1, "human", "q?")
        asyncio.run(worker.engine.chat(job, "q?"))
        last = worker.store.chat_last("feat-c4", 2)
        assert last["role"] == "engine"
        assert last["degraded"] == 1
        assert "moved on" in last["text"]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test")

    from app import config

    config.get_settings.cache_clear()
    import importlib

    from app import main as main_module

    importlib.reload(main_module)
    with TestClient(main_module.app) as c:
        yield c, main_module
    config.get_settings.cache_clear()


class TestChatEndpoints:
    def _park_feature(self, main_module, job_id="feat-api1"):
        store = main_module.app.state.store
        worker = main_module.app.state.worker
        worker.intake_feature(job_id, title="F", project="web", request="r")
        store.set_fields(job_id, stage=3, stage_attempts=1)
        store.set_status(job_id, "awaiting_input")
        return store

    def test_post_validations(self, client):
        c, m = client
        r = c.post("/api/jobs/none/chat", headers=AUTH, json={"message": "hi"})
        assert r.status_code == 404
        store = self._park_feature(m)
        r = c.post("/api/jobs/feat-api1/chat", headers=AUTH, json={"message": "  "})
        assert r.status_code == 400
        store.set_status("feat-api1", "running")
        r = c.post("/api/jobs/feat-api1/chat", headers=AUTH, json={"message": "hi"})
        assert r.status_code == 409

    def test_chat_only_for_features(self, client):
        c, m = client
        store = m.app.state.store
        m.app.state.worker.intake_task("task-api1", title="T", project="web", request="r")
        store.set_status("task-api1", "awaiting_input")
        r = c.post("/api/jobs/task-api1/chat", headers=AUTH, json={"message": "hi"})
        assert r.status_code == 409
        assert "feature" in r.json()["detail"]

    def test_single_flight_and_get(self, client):
        c, m = client
        store = self._park_feature(m, "feat-api2")
        r = c.post("/api/jobs/feat-api2/chat", headers=AUTH, json={"message": "why B?"})
        assert r.status_code == 202
        # second message while the first is unanswered -> 409
        r = c.post("/api/jobs/feat-api2/chat", headers=AUTH, json={"message": "and C?"})
        assert r.status_code == 409
        g = c.get("/api/jobs/feat-api2/chat", headers=AUTH).json()
        assert g["pending"] is True
        assert g["turns"][-1]["role"] == "human"
        assert g["turns"][-1].get("pending") is True

    def test_turn_limit(self, client):
        c, m = client
        store = self._park_feature(m, "feat-api3")
        for i in range(m.settings.chat_max_turns_per_gate):
            store.chat_add("feat-api3", 3, 1, "human", f"q{i}")
            store.chat_add("feat-api3", 3, 1, "engine", f"a{i}")
        r = c.post("/api/jobs/feat-api3/chat", headers=AUTH, json={"message": "one more"})
        assert r.status_code == 409
        assert "limit" in r.json()["detail"]
        g = c.get("/api/jobs/feat-api3/chat", headers=AUTH).json()
        assert g["limit_reached"] is True

    def test_stale_pending_clears_after_timeout(self, client):
        c, m = client
        store = self._park_feature(m, "feat-api4")
        store.chat_add("feat-api4", 3, 1, "human", "orphaned")
        # backdate the orphaned turn past timeout + grace
        with store._conn() as conn:
            conn.execute("UPDATE gate_chat SET at = ? WHERE job_id = 'feat-api4'",
                         (time.time() - m.settings.chat_timeout_seconds - 120,))
        g = c.get("/api/jobs/feat-api4/chat", headers=AUTH).json()
        assert g["pending"] is False
        r = c.post("/api/jobs/feat-api4/chat", headers=AUTH, json={"message": "retry"})
        assert r.status_code == 202

    def test_stats_includes_chat(self, client):
        c, m = client
        self._park_feature(m, "feat-api5")
        m.app.state.store.chat_add("feat-api5", 3, 1, "human", "q")
        r = c.get("/api/features/feat-api5/stats", headers=AUTH)
        assert r.status_code == 200
        assert len(r.json()["chat"]) == 1

import asyncio
import base64
import importlib

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.fixer import RawRunResult
from app.worker import Worker


AUTH = {"Authorization": "Basic " + base64.b64encode(b"gumo:test").decode()}


def _persist_worker(store, tmp_path):
    s = Settings(data_dir=str(tmp_path), dashboard_password="test", session_persistence=True)
    return Worker(s, store)


class TestRequestSteer:
    def test_interrupts_a_live_run_with_persistence(self, store, tmp_path):
        """A running stage + session persistence: the note is stored and the
        interrupt event is tripped so the stage resumes it in place."""
        w = _persist_worker(store, tmp_path)
        w.intake_feature("feat-s1", title="F", project="web", request="r")
        store.set_fields("feat-s1", stage=5)
        store.set_status("feat-s1", "running")
        ev = asyncio.Event()
        w.engine._steer["feat-s1"] = {"event": ev, "stage": 5}

        assert w.request_steer("feat-s1", "use the v2 endpoint") == "interrupting"
        assert ev.is_set()
        assert store.get("feat-s1")["steer_note"] == "use the v2 endpoint"

    def test_queues_when_not_running(self, store, tmp_path):
        """No live run: the note lands as guidance for the next checkpoint."""
        w = _persist_worker(store, tmp_path)
        w.intake_feature("feat-s2", title="F", project="web", request="r")
        store.set_fields("feat-s2", stage=3)
        store.set_status("feat-s2", "awaiting_input")

        assert w.request_steer("feat-s2", "prefer soft deletes") == "queued"
        guidance = [g for g in store.guidance_for("feat-s2") if g["action"] == "steer"]
        assert guidance and guidance[-1]["text"] == "prefer soft deletes"

    def test_queues_when_persistence_off_even_if_running(self, store, tmp_path):
        """Running but persistence off: cannot resume, so fall back to guidance."""
        s = Settings(data_dir=str(tmp_path), dashboard_password="test",
                     session_persistence=False)
        w = Worker(s, store)
        w.intake_feature("feat-s3", title="F", project="web", request="r")
        store.set_fields("feat-s3", stage=5)
        store.set_status("feat-s3", "running")
        w.engine._steer["feat-s3"] = {"event": asyncio.Event(), "stage": 5}

        assert w.request_steer("feat-s3", "x") == "queued"
        assert not w.engine._steer["feat-s3"]["event"].is_set()

    def test_empty_note_is_rejected(self, store, tmp_path):
        w = _persist_worker(store, tmp_path)
        w.intake_feature("feat-s4", title="F", project="web", request="r")
        assert w.request_steer("feat-s4", "   ") == "empty"

    def test_non_feature_raises(self, worker):
        worker.store.insert("sentry-1", source="manual", kind="sentry")
        with pytest.raises(ValueError):
            worker.request_steer("sentry-1", "note")


class TestSteerReenqueue:
    def test_sets_resume_fields_and_requeues(self, store, tmp_path, monkeypatch):
        """An interrupted run checkpoints, then arms a 'steer' resume of the same
        session with the note as the answer, and re-enqueues."""
        import app.engine as engine_mod

        w = _persist_worker(store, tmp_path)
        eng = w.engine
        w.intake_feature("feat-r1", title="F", project="web", request="r")
        store.set_fields("feat-r1", stage=5, steer_note="switch to a queue")
        store.set_status("feat-r1", "running")
        run_id = store.stage_run_open("feat-r1", 5, 1)
        job = store.get("feat-r1")

        async def truthy(*a, **k):
            return True

        async def fake_git(ws, *args):
            return (0, "headsha999")

        async def anoop(*a, **k):
            return None

        monkeypatch.setattr(eng, "_checkpoint", truthy)
        monkeypatch.setattr(engine_mod, "git", fake_git)
        monkeypatch.setattr(eng, "_comment", anoop)

        raw = RawRunResult("interrupted", "partial", {"session_id": "sess-live"})
        out = asyncio.run(eng._steer_reenqueue(job, 5, run_id, 1, str(tmp_path),
                                               "brain/feat-feat-r1", raw,
                                               lambda e, d: None))
        assert out == "requeue"
        j = store.get("feat-r1")
        assert j["status"] == "queued"
        assert j["gate_kind"] == "steer"
        assert j["resume_session_id"] == "sess-live"
        assert j["resume_answer"] == "switch to a queue"
        assert j["resume_stage"] == 5
        assert j["resume_head"] == "headsha999"
        assert j["steer_note"] == ""

    def test_reenqueue_survives_comment_failure(self, store, tmp_path, monkeypatch):
        """Seer PR#6 round 2: a failing best-effort _comment after the run is closed
        'interrupted' and the job requeued must NOT propagate — otherwise run_stage's
        handler flips the job to error and re-closes the run 'exception', losing the
        steer resume."""
        import app.engine as engine_mod

        w = _persist_worker(store, tmp_path)
        eng = w.engine
        w.intake_feature("feat-r2", title="F", project="web", request="r")
        store.set_fields("feat-r2", stage=5, steer_note="switch to a queue")
        store.set_status("feat-r2", "running")
        run_id = store.stage_run_open("feat-r2", 5, 1)
        job = store.get("feat-r2")

        async def truthy(*a, **k):
            return True

        async def fake_git(ws, *args):
            return (0, "headsha999")

        async def boom(*a, **k):
            raise RuntimeError("clickup down")

        monkeypatch.setattr(eng, "_checkpoint", truthy)
        monkeypatch.setattr(engine_mod, "git", fake_git)
        monkeypatch.setattr(eng.clickup, "comment", boom)  # _comment must swallow it

        raw = RawRunResult("interrupted", "partial", {"session_id": "sess-live"})
        out = asyncio.run(eng._steer_reenqueue(job, 5, run_id, 1, str(tmp_path),
                                               "brain/feat-feat-r2", raw,
                                               lambda e, d: None))
        assert out == "requeue"
        j = store.get("feat-r2")
        assert j["status"] == "queued"          # not flipped to error
        assert j["gate_kind"] == "steer"
        assert store.stage_runs_for("feat-r2")[-1]["result_status"] == "interrupted"

    def test_stage_run_close_first_close_wins(self, store):
        """Seer PR#6 round 2: a second close must not overwrite the first — so a
        late 'exception' can't corrupt an 'interrupted' (or any) final status."""
        rid = store.stage_run_open("feat-cl", 5, 1)
        store.stage_run_close(rid, "interrupted")
        store.stage_run_close(rid, "exception")  # ignored — already closed
        assert store.stage_runs_for("feat-cl")[-1]["result_status"] == "interrupted"

    def test_resume_intended_accepts_steer(self, store, tmp_path):
        eng = _persist_worker(store, tmp_path).engine
        job = {"gate_kind": "steer", "resume_session_id": "s", "resume_stage": 5,
               "resume_answer": "note"}
        assert eng._resume_intended(job, 5) is True
        # wrong stage / missing answer do not qualify
        assert eng._resume_intended({**job, "resume_stage": 4}, 5) is False
        assert eng._resume_intended({**job, "resume_answer": ""}, 5) is False


class TestSessionRoutes:
    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DASHBOARD_PASSWORD", "test")
        from app import config
        config.get_settings.cache_clear()
        from app import main as main_module
        importlib.reload(main_module)
        with TestClient(main_module.app) as c:
            c._main = main_module
            yield c
        config.get_settings.cache_clear()

    def _feature(self, client, job_id, status="running", stage=5):
        store = client._main.app.state.store
        worker = client._main.app.state.worker
        worker.intake_feature(job_id, title="Multi-currency", project="web", request="r")
        store.set_fields(job_id, stage=stage, stage_attempts=1)
        store.set_status(job_id, status)
        return store, worker

    def test_snapshot_shape(self, client):
        store, _ = self._feature(client, "feat-a1")
        store.chat_add("feat-a1", 5, 1, "human", "how is it going?")
        store.chat_add("feat-a1", 5, 1, "engine", "building the Money object")
        r = client.get("/api/jobs/feat-a1/session", headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["job"]["stage"] == 5
        assert data["job"]["stage_name"]
        assert data["live"] is True
        assert data["steer_available"] is True
        assert {"runs", "guidance", "artifacts", "chat", "chat_pending", "chat_limit"} <= data.keys()
        # the snapshot carries the FULL conversation (the inbox thread)
        assert [t["role"] for t in data["chat"]] == ["human", "engine"]
        assert data["chat_pending"] is False

    def test_snapshot_serves_all_kinds(self, client):
        assert client.get("/api/jobs/nope/session", headers=AUTH).status_code == 404
        store = client._main.app.state.store
        store.insert("sen-1", source="manual", kind="sentry")
        store.set_fields("sen-1", analysis="root cause: X", question="raise limit?")
        store.set_status("sen-1", "awaiting_input")
        r = client.get("/api/jobs/sen-1/session", headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["job"]["kind"] == "sentry"
        assert data["job"]["analysis"] == "root cause: X"
        assert data["runs"] == [] and data["artifacts"] == []
        assert data["chat_available"] is True
        assert data["steer_available"] is False  # steering stays feature-only

    def test_steer_endpoint_queues(self, client):
        # not running -> queued (guidance), 202
        self._feature(client, "feat-a2", status="awaiting_input", stage=3)
        r = client.post("/api/jobs/feat-a2/session/steer", headers=AUTH,
                        json={"note": "use soft deletes"})
        assert r.status_code == 202
        assert r.json()["status"] == "queued"

    def test_steer_validation(self, client):
        self._feature(client, "feat-a3")
        assert client.post("/api/jobs/feat-a3/session/steer", headers=AUTH,
                           json={"note": "  "}).status_code == 400
        assert client.post("/api/jobs/nope/session/steer", headers=AUTH,
                           json={"note": "x"}).status_code == 404

    def test_steer_requires_auth(self, client):
        assert client.post("/api/jobs/x/session/steer", json={"note": "x"}).status_code == 401
        assert client.get("/api/jobs/x/session").status_code == 401

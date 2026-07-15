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


class TestChatCancellation:
    def test_cancelled_chat_leaves_tombstone_engine_turn(self, worker, monkeypatch):
        """Seer round 8: a chat task cancelled at shutdown must not orphan its
        human turn — an orphan reads as an answer in flight and blocks the
        gate's chat for the stale-pending window after restart."""
        worker.intake_feature("feat-cx1", title="F", project="web", request="r")
        worker.store.set_fields("feat-cx1", stage=3, stage_attempts=1)
        worker.store.set_status("feat-cx1", "awaiting_input")
        job = worker.store.get("feat-cx1")
        worker.store.chat_add("feat-cx1", 3, 1, "human", "q?")

        started = asyncio.Event()

        async def hang(*a, **k):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(worker.engine, "_chat_inner", hang)

        async def run():
            task = asyncio.create_task(worker.engine.chat(job, "q?"))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        last = worker.store.chat_last("feat-cx1", 3)
        assert last["role"] == "engine"
        assert last["degraded"] == 1


class TestChatConfigStoreLock:
    """Seer round 7: an artifact-primed chat writes to a claude config store and
    must hold the lock guarding THAT store while it runs — chat_global for the
    dedicated chat store (persistence on), claude_global for the shared default
    store (persistence off, where stage runs also live)."""

    def _run_chat(self, worker, monkeypatch, tmp_path, job_id):
        import app.engine as engine_mod

        eng = worker.engine
        worker.intake_feature(job_id, title="F", project="web", request="r")
        worker.store.set_fields(job_id, stage=3, stage_attempts=1)
        worker.store.set_status(job_id, "awaiting_input")
        job = worker.store.get(job_id)
        worker.store.chat_add(job_id, 3, 1, "human", "q?")

        seen = {}

        async def fake_prepare(*a, **k):
            return str(tmp_path)

        async def fake_git(*a, **k):
            return (0, "")

        async def fake_raw(settings, workspace, prompt, allowed_tools, timeout, **kw):
            seen["claude_global"] = eng.locks.claude_global.locked()
            seen["chat_global"] = eng.locks.chat_global.locked()
            seen["config_dir"] = kw.get("config_dir")

            class R:
                status = "ok"
                text = "answer"
                meta = {}

            return R()

        monkeypatch.setattr(engine_mod, "prepare_feature_workspace", fake_prepare)
        monkeypatch.setattr(engine_mod, "git", fake_git)
        monkeypatch.setattr(engine_mod, "run_claude_stream", fake_raw)
        asyncio.run(eng.chat(job, "q?"))
        assert worker.store.chat_last(job_id, 3)["degraded"] == 0
        return seen

    def test_persistence_off_holds_shared_default_store_lock(
            self, worker, monkeypatch, tmp_path):
        seen = self._run_chat(worker, monkeypatch, tmp_path, "feat-lk1")
        assert seen["config_dir"] is None
        assert seen["claude_global"] is True   # shared with stage runs
        assert seen["chat_global"] is False

    def test_persistence_on_holds_chat_store_lock(self, tmp_path, monkeypatch):
        from app.config import Settings
        from app.db import JobStore
        from app.worker import Worker

        s = Settings(data_dir=str(tmp_path), dashboard_password="test",
                     session_persistence=True)
        w = Worker(s, JobStore(str(tmp_path / "brain.db")))
        seen = self._run_chat(w, monkeypatch, tmp_path, "feat-lk2")
        assert seen["config_dir"] == s.claude_chat_config_dir
        assert seen["chat_global"] is True     # dedicated chat store
        assert seen["claude_global"] is False  # stage runs stay unblocked


class TestTwoLaneDispatch:
    """docs/CONVERSATIONS.md §5: fast lane answers when it can, escalates on
    NEED_CODE_RUN, and errors fall through — chat never loses an answer to the
    fast lane."""

    def _worker(self, tmp_path):
        from app.config import Settings
        from app.db import JobStore
        from app.worker import Worker

        s = Settings(data_dir=str(tmp_path), dashboard_password="test",
                     chat_fast_model="claude-sonnet-5", chat_api_key="k-test")
        return Worker(s, JobStore(str(tmp_path / "brain.db")))

    def _gate(self, worker, job_id):
        worker.intake_feature(job_id, title="F", project="web", request="r")
        worker.store.set_fields(job_id, stage=3, stage_attempts=1,
                                analysis="chose option B", question="1. ok?")
        worker.store.set_status(job_id, "awaiting_input")
        worker.store.chat_add(job_id, 3, 1, "human", "why B?")
        return worker.store.get(job_id)

    def test_fast_lane_ok_skips_slow_lane(self, tmp_path, monkeypatch):
        import app.engine as engine_mod

        w = self._worker(tmp_path)
        job = self._gate(w, "feat-2l1")
        events = []

        async def fake_fast(settings, system, messages, on_delta):
            on_delta("because "); on_delta("B avoids a migration")
            return "ok", "because B avoids a migration", {"lane": "fast", "duration_ms": 900}

        async def no_slow(*a, **k):
            raise AssertionError("slow lane must not run on a fast-lane answer")

        monkeypatch.setattr(engine_mod.fastlane, "stream_answer", fake_fast)
        monkeypatch.setattr(w.engine, "_chat_inner", no_slow)
        asyncio.run(w.engine.chat(job, "why B?",
                                  publish=lambda e, d: events.append((e, d))))
        last = w.store.chat_last("feat-2l1", 3)
        assert last["role"] == "engine"
        assert last["lane"] == "fast"
        assert last["degraded"] == 0
        assert "migration" in last["text"]
        assert ("delta", "because ") in events

    def test_escalation_reaches_slow_lane(self, tmp_path, monkeypatch):
        import app.engine as engine_mod

        w = self._worker(tmp_path)
        job = self._gate(w, "feat-2l2")
        events, called = [], {}

        async def fake_fast(settings, system, messages, on_delta):
            return "escalate", "NEED_CODE_RUN: must read billing code", {"lane": "fast"}

        async def fake_slow(job_, stage, message, target, publish=None):
            called["slow"] = True
            return "from the code: yes", {"session_id": "s1"}, False

        monkeypatch.setattr(engine_mod.fastlane, "stream_answer", fake_fast)
        monkeypatch.setattr(w.engine, "_chat_inner", fake_slow)
        asyncio.run(w.engine.chat(job, "does it handle refunds?",
                                  publish=lambda e, d: events.append((e, d))))
        assert called.get("slow") is True
        last = w.store.chat_last("feat-2l2", 3)
        assert last["text"] == "from the code: yes"
        assert last["lane"] == ""
        statuses = [d for e, d in events if e == "status"]
        assert any("code run" in s for s in statuses)

    def test_fast_error_falls_through(self, tmp_path, monkeypatch):
        import app.engine as engine_mod

        w = self._worker(tmp_path)
        job = self._gate(w, "feat-2l3")

        async def fake_fast(settings, system, messages, on_delta):
            return "error", "HTTP 529: overloaded", {"lane": "fast"}

        async def fake_slow(job_, stage, message, target, publish=None):
            return "slow lane answer", {}, False

        monkeypatch.setattr(engine_mod.fastlane, "stream_answer", fake_fast)
        monkeypatch.setattr(w.engine, "_chat_inner", fake_slow)
        asyncio.run(w.engine.chat(job, "why B?"))
        assert w.store.chat_last("feat-2l3", 3)["text"] == "slow lane answer"

    def test_disabled_without_model_goes_straight_to_slow(self, worker, monkeypatch):
        # the default worker fixture has chat_fast_model="" -> disabled
        called = {}

        async def fake_slow(job_, stage, message, target, publish=None):
            called["slow"] = True
            return "answer", {}, False

        async def no_fast(*a, **k):
            raise AssertionError("fast lane must not run when disabled")

        import app.engine as engine_mod

        worker.intake_feature("feat-2l4", title="F", project="web", request="r")
        worker.store.set_fields("feat-2l4", stage=3, stage_attempts=1)
        worker.store.set_status("feat-2l4", "awaiting_input")
        worker.store.chat_add("feat-2l4", 3, 1, "human", "q?")
        monkeypatch.setattr(engine_mod.fastlane, "stream_answer", no_fast)
        monkeypatch.setattr(worker.engine, "_chat_inner", fake_slow)
        asyncio.run(worker.engine.chat(worker.store.get("feat-2l4"), "q?"))
        assert called.get("slow") is True


class TestChatBroker:
    def test_replay_then_live_then_done(self):
        from app.chatstream import ChatBroker

        async def run():
            b = ChatBroker()
            b.start("j1")
            b.publish("j1", "delta", "hel")
            b.publish("j1", "status", "reading x")
            got = []

            async def consume():
                async for ev in b.subscribe("j1"):
                    got.append(ev)

            task = asyncio.create_task(consume())
            await asyncio.sleep(0.01)      # subscriber replays, then waits live
            b.publish("j1", "delta", "lo")
            b.finish("j1")
            await asyncio.wait_for(task, timeout=2)
            return got

        got = asyncio.run(run())
        assert got == [("delta", "hel"), ("status", "reading x"),
                       ("delta", "lo"), ("done", "")]

    def test_subscribe_after_finish_replays_and_ends(self):
        from app.chatstream import ChatBroker

        async def run():
            b = ChatBroker()
            b.start("j2")
            b.publish("j2", "delta", "answer")
            b.finish("j2")
            return [ev async for ev in b.subscribe("j2")]

        got = asyncio.run(run())
        assert got == [("delta", "answer"), ("done", "")]

    def test_no_turn_yields_done_immediately(self):
        from app.chatstream import ChatBroker

        async def run():
            return [ev async for ev in ChatBroker().subscribe("nope")]

        assert asyncio.run(run()) == [("done", "")]

    def test_new_turn_closes_previous_subscribers(self):
        from app.chatstream import ChatBroker

        async def run():
            b = ChatBroker()
            b.start("j3")
            got = []

            async def consume():
                async for ev in b.subscribe("j3"):
                    got.append(ev)

            task = asyncio.create_task(consume())
            await asyncio.sleep(0.01)
            b.start("j3")  # next POST — old stream must end, not leak
            await asyncio.wait_for(task, timeout=2)
            return got

        assert asyncio.run(run()) == [("done", "")]

    def test_delta_coalescing_past_cap(self):
        import app.chatstream as cs

        b = cs.ChatBroker()
        b.start("j4")
        for i in range(cs.MAX_EVENTS + 50):
            b.publish("j4", "delta", "x")
        t = b._turns["j4"]
        assert len(t["events"]) == cs.MAX_EVENTS
        assert t["events"][-1][1].endswith("x" * 50)


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
        # mid-run chat is allowed (the inbox conversation) — 202, not 409
        store.set_status("feat-api1", "running")
        r = c.post("/api/jobs/feat-api1/chat", headers=AUTH, json={"message": "hi"})
        assert r.status_code == 202
        # but a finished job has no one to talk to
        store.set_status("feat-api1", "pr_opened")
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

    def test_pending_scoped_per_attempt(self, client):
        """Seer PR#8 round 4: an unanswered question from before a redo must not
        wedge the fresh gate's chat for the stale-pending window."""
        c, m = client
        store = self._park_feature(m, "feat-api7")
        store.chat_add("feat-api7", 3, 1, "human", "asked just before the redo")
        # redo lands: same stage, new attempt
        store.set_fields("feat-api7", stage_attempts=2)
        g = c.get("/api/jobs/feat-api7/chat", headers=AUTH).json()
        assert g["pending"] is False
        r = c.post("/api/jobs/feat-api7/chat", headers=AUTH, json={"message": "fresh gate q"})
        assert r.status_code == 202

    def test_turn_limit_resets_per_attempt(self, client):
        """Seer PR#8 round 3: the budget is per GATE (attempt) — a redo parks a
        new gate, so turns spent on the rejected attempt must not starve it."""
        c, m = client
        store = self._park_feature(m, "feat-api6")
        for i in range(m.settings.chat_max_turns_per_gate):
            store.chat_add("feat-api6", 3, 1, "human", f"q{i}")
            store.chat_add("feat-api6", 3, 1, "engine", f"a{i}")
        # redo: the stage re-runs and parks again as attempt 2
        store.set_fields("feat-api6", stage_attempts=2)
        g = c.get("/api/jobs/feat-api6/chat", headers=AUTH).json()
        assert g["limit_reached"] is False
        r = c.post("/api/jobs/feat-api6/chat", headers=AUTH, json={"message": "fresh gate q"})
        assert r.status_code == 202

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

    def test_chat_stream_replays_and_ends(self, client):
        """SSE endpoint (docs/CONVERSATIONS.md §5): a finished turn replays its
        buffered events and terminates with done — a late subscriber never hangs."""
        c, m = client
        self._park_feature(m, "feat-api6")
        m.chat_broker.start("feat-api6")
        m.chat_broker.publish("feat-api6", "delta", "hi there")
        m.chat_broker.publish("feat-api6", "status", "Read app/x.py")
        m.chat_broker.finish("feat-api6")
        r = c.get("/api/jobs/feat-api6/chat/stream", headers=AUTH)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert "event: delta" in r.text and '"t": "hi there"' in r.text
        assert "event: status" in r.text
        assert r.text.rstrip().endswith('data: {"t": ""}')  # done is last
        assert c.get("/api/jobs/none/chat/stream", headers=AUTH).status_code == 404

    def test_post_starts_stream_turn(self, client):
        c, m = client
        self._park_feature(m, "feat-api7")
        r = c.post("/api/jobs/feat-api7/chat", headers=AUTH, json={"message": "why?"})
        assert r.status_code == 202
        # the broker turn exists the moment the POST returns (started before the task)
        assert "feat-api7" in m.chat_broker._turns

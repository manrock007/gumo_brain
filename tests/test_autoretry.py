"""Transient-error auto-retry: a run that dies on an upstream hiccup (API 5xx,
overloaded, connection reset) gets ONE automatic requeue; run-produced errors
and timeouts still park for a human /redo. The budget is per stage/phase —
restored when a gate parks or a human redo lands — so nothing retry-loops."""

import asyncio

from app.fixer import TRANSIENT_ERROR_RE, RawRunResult


class TestTransientDetector:
    def test_matches_upstream_hiccups(self):
        for text in ("claude exited 1: API Error: Server error mid-response",
                     "API Error: 529 overloaded_error",
                     "connection reset by peer",
                     "502 Bad Gateway", "rate limit exceeded"):
            assert TRANSIENT_ERROR_RE.search(text), text

    def test_ignores_run_produced_errors(self):
        for text in ("STAGE_FAIL: acceptance criteria unmet",
                     "assertion failed in test_x",
                     "git checkout failed: local changes",
                     "stream ended without a result envelope"):
            assert not TRANSIENT_ERROR_RE.search(text), text


class TestFeatureStageAutoRetry:
    def _job(self, worker, job_id):
        worker.intake_feature(job_id, title="F", project="web", request="r")
        worker.store.set_fields(job_id, stage=9, stage_attempts=1)
        worker.store.set_status(job_id, "running")
        return worker.store.get(job_id)

    def test_transient_error_requeues_once(self, worker):
        job = self._job(worker, "feat-ar1")
        run_id = worker.store.stage_run_open("feat-ar1", 9, 1)
        raw = RawRunResult("error", "API Error: Server error mid-response", {})
        out = asyncio.run(worker.engine._after_run(
            job, 9, run_id, None, "b", "/nonexistent", raw, ""))
        assert out == "requeue"
        fresh = worker.store.get("feat-ar1")
        assert fresh["status"] == "queued"
        assert fresh["auto_retries"] == 1

    def test_second_transient_error_parks_as_error(self, worker):
        job = self._job(worker, "feat-ar2")
        worker.store.set_fields("feat-ar2", auto_retries=1)  # budget spent
        job = worker.store.get("feat-ar2")
        run_id = worker.store.stage_run_open("feat-ar2", 9, 1)
        raw = RawRunResult("error", "API Error: Server error mid-response", {})
        out = asyncio.run(worker.engine._after_run(
            job, 9, run_id, None, "b", "/nonexistent", raw, ""))
        assert out is None
        assert worker.store.get("feat-ar2")["status"] == "error"

    def test_run_produced_error_never_retries(self, worker):
        job = self._job(worker, "feat-ar3")
        run_id = worker.store.stage_run_open("feat-ar3", 9, 1)
        raw = RawRunResult("error", "assertion failed: bad artifact", {})
        out = asyncio.run(worker.engine._after_run(
            job, 9, run_id, None, "b", "/nonexistent", raw, ""))
        assert out is None
        assert worker.store.get("feat-ar3")["status"] == "error"

    def test_timeout_never_retries(self, worker):
        job = self._job(worker, "feat-ar4")
        run_id = worker.store.stage_run_open("feat-ar4", 9, 1)
        raw = RawRunResult("timeout", "API Error: Server error (but a timeout)", {})
        out = asyncio.run(worker.engine._after_run(
            job, 9, run_id, None, "b", "/nonexistent", raw, ""))
        assert out is None
        assert worker.store.get("feat-ar4")["status"] == "timeout"

    def test_human_redo_restores_the_budget(self, worker):
        self._job(worker, "feat-ar5")
        worker.store.set_fields("feat-ar5", auto_retries=1)
        worker.store.set_status("feat-ar5", "error")
        asyncio.run(worker.answer_job("feat-ar5", "redo", "", via="dashboard"))
        assert worker.store.get("feat-ar5")["auto_retries"] == 0


class TestV1AutoRetry:
    def test_transient_error_requeues_once(self, worker):
        worker.intake_task("task-ar1", title="T", project="web", request="r")
        row = worker.store.get("task-ar1")
        armed = asyncio.run(worker._maybe_auto_retry_v1(
            row, "error", "claude exited 1: API Error: Server error", ""))
        assert armed is True
        fresh = worker.store.get("task-ar1")
        assert fresh["status"] == "queued" and fresh["auto_retries"] == 1
        # budget spent: the same failure now parks as error (caller handles)
        armed = asyncio.run(worker._maybe_auto_retry_v1(
            fresh, "error", "API Error: Server error", ""))
        assert armed is False

    def test_non_transient_and_timeout_never_retry(self, worker):
        worker.intake_task("task-ar2", title="T", project="web", request="r")
        row = worker.store.get("task-ar2")
        assert asyncio.run(worker._maybe_auto_retry_v1(
            row, "error", "NO_FIX-ish real failure", "")) is False
        assert asyncio.run(worker._maybe_auto_retry_v1(
            row, "timeout", "API Error: Server error", "")) is False

    def test_park_restores_the_budget(self, worker):
        worker.intake_task("task-ar3", title="T", project="web", request="r")
        worker.store.set_fields("task-ar3", auto_retries=1)
        asyncio.run(worker._park_awaiting("task-ar3", "", "analysis text"))
        assert worker.store.get("task-ar3")["auto_retries"] == 0

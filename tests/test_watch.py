"""The post-ship watcher (Epic B4): daily reads, the deadline finish, the
verdict formula, and the crash-safe Iterate-gate park ordering."""

import asyncio
import json
import time

import pytest

from app.outcome import build_gate_packet, compute_verdict, parse_direction
from app.worker import GateConflict


class ScriptedProvider:
    """query_metric returns the scripted result; every call is recorded."""

    name = "scripted"

    def __init__(self, result=None, by_end=None):
        self.result = result or {"status": "ok", "series": [], "total": 42.0,
                                 "detail": "scripted"}
        self.by_end = by_end or {}  # end (rounded) -> result, for baseline calls
        self.calls = []

    async def query_metric(self, metric, window_days, *, event="", end=None):
        self.calls.append({"metric": metric, "window_days": window_days,
                           "event": event, "end": end})
        if end is not None and self.by_end:
            return self.by_end.get("baseline", self.result)
        return self.result


def _watch(worker, job_id="watch-feat-w1", *, deadline_offset=86400,
           started_offset=-3600, metric="signups", event="signup_done",
           target="", window=7, task_id=""):
    now = time.time()
    worker.store.watch_insert(
        job_id, title="watch: F", project="web",
        related_jobs=job_id.removeprefix("watch-"),
        success_metric=metric, metric_target=target, metric_event=event,
        metric_window_days=window,
        watch_started_at=now + started_offset,
        watch_deadline=now + deadline_offset,
        owner="111", clickup_task_id=task_id)
    return worker.store.get(job_id)


class TestComputeVerdict:
    BAND = 10

    def _readings(self, *vals):
        return [{"observed": v, "window_day": i + 1} for i, v in enumerate(vals)]

    def test_no_readings_is_unmeasured(self):
        v, inputs = compute_verdict([], "100", 50.0, self.BAND)
        assert v == "unmeasured"
        assert "no successful readings" in inputs["rule"]

    def test_increase_target_met_is_moved(self):
        v, inputs = compute_verdict(self._readings(80.0, 120.0), ">= 100", None, self.BAND)
        assert v == "moved"
        assert inputs["observed"] == 120.0 and inputs["direction"] == "up"

    def test_target_missed_below_baseline_band_is_regressed(self):
        v, _ = compute_verdict(self._readings(60.0), "at least 100", 80.0, self.BAND)
        assert v == "regressed"  # 60 < 80*(1-0.10)=72, explicit increase goal

    def test_target_missed_in_band_is_flat(self):
        v, _ = compute_verdict(self._readings(75.0), ">= 100", 80.0, self.BAND)
        assert v == "flat"  # within ±10% of baseline 80

    def test_decrease_goal_met_is_moved_never_regressed(self):
        """Amendment 8: an error-rate reduction hitting its 'under' target is
        MOVED — the naive observed>=target rule would have called it a miss."""
        v, inputs = compute_verdict(self._readings(0.5), "under 1.0", 2.0, self.BAND)
        assert v == "moved"
        assert inputs["direction"] == "down"

    def test_decrease_goal_missed_and_worse_is_regressed(self):
        v, _ = compute_verdict(self._readings(3.0), "<= 1.0", 2.0, self.BAND)
        assert v == "regressed"  # 3.0 > 2.0*(1+0.10)

    def test_ambiguous_direction_never_asserts_regression(self):
        """A bare numeric target with no direction cue: a successful reduction
        must not be mislabeled 'regressed' — downgraded to flat, rule recorded."""
        v, inputs = compute_verdict(self._readings(0.5), "1.0", 2.0, self.BAND)
        assert v == "flat"
        assert "regression not asserted" in inputs["rule"]

    def test_no_target_with_baseline_uses_the_band(self):
        assert compute_verdict(self._readings(95.0), "", 100.0, self.BAND)[0] == "flat"
        assert compute_verdict(self._readings(150.0), "", 100.0, self.BAND)[0] == "moved"
        assert compute_verdict(self._readings(50.0), "", 100.0, self.BAND)[0] == "regressed"

    def test_no_target_no_baseline_is_unmeasured(self):
        v, inputs = compute_verdict(self._readings(50.0), "", None, self.BAND)
        assert v == "unmeasured"
        assert "no baseline" in inputs["rule"]

    def test_non_numeric_target_with_baseline(self):
        v, _ = compute_verdict(self._readings(150.0), "grow signups", 100.0, self.BAND)
        assert v == "moved"
        v, _ = compute_verdict(self._readings(50.0), "reduce errors", 100.0, self.BAND)
        assert v == "moved"  # decrease goal, observed well under baseline

    def test_direction_parser(self):
        assert parse_direction(">= 40") == "up"
        assert parse_direction("<= 300ms") == "down"
        assert parse_direction("under 2%") == "down"
        assert parse_direction("at least 10") == "up"
        assert parse_direction("100") == ""


class TestGatePacket:
    def test_packet_shows_rule_table_and_verbs(self):
        fields = {"verdict": "moved", "metric": "signups", "metric_event": "signup_done",
                  "target": ">= 100", "observed": 120.0, "baseline": 80.0,
                  "window_days": 7,
                  "verdict_inputs": json.dumps({"rule": "increase goal met: x"})}
        readings = [{"window_day": 1, "observed": 50.0, "detail": "d1"},
                    {"window_day": 2, "observed": 120.0, "detail": "d2"}]
        packet = build_gate_packet({"issue_id": "watch-x"}, fields, readings)
        assert "Outcome verdict: moved" in packet
        assert "increase goal met" in packet
        assert "| 2 | 120.0" in packet
        assert "## Questions" in packet
        assert "/proceed" in packet and "/redo <days>" in packet and "/skip" in packet


class TestWatchPass:
    def test_daily_read_records_and_throttles(self, worker, monkeypatch):
        provider = ScriptedProvider()
        monkeypatch.setattr(worker, "_analytics_provider_for", lambda job: provider)
        _watch(worker, "watch-feat-t1")
        asyncio.run(worker._watch_pass())
        readings = worker.store.readings_for("watch-feat-t1")
        assert len(readings) == 1
        assert readings[0]["observed"] == 42.0
        assert readings[0]["window_day"] == 1
        assert readings[0]["window_start"] == worker.store.get("watch-feat-t1")["watch_started_at"]
        # a second pass within 22h must NOT read again
        asyncio.run(worker._watch_pass())
        assert len(worker.store.readings_for("watch-feat-t1")) == 1
        assert len(provider.calls) == 1

    def test_provider_error_leaves_visible_detail_no_fake_reading(self, worker, monkeypatch):
        provider = ScriptedProvider(result={"status": "error", "series": [],
                                            "total": None, "detail": "mixpanel HTTP 401"})
        monkeypatch.setattr(worker, "_analytics_provider_for", lambda job: provider)
        _watch(worker, "watch-feat-t2")
        asyncio.run(worker._watch_pass())
        assert worker.store.readings_for("watch-feat-t2") == []
        assert "401" in worker.store.get("watch-feat-t2")["detail"]

    def test_deadline_crossing_finishes_and_parks(self, worker, monkeypatch):
        provider = ScriptedProvider(
            result={"status": "ok", "series": [], "total": 120.0, "detail": "final"},
            by_end={"baseline": {"status": "ok", "series": [], "total": 80.0,
                                 "detail": "baseline"}})
        monkeypatch.setattr(worker, "_analytics_provider_for", lambda job: provider)
        _watch(worker, "watch-feat-t3", deadline_offset=-60,
               started_offset=-7 * 86400, target=">= 100")
        asyncio.run(worker._watch_pass())
        row = worker.store.get("watch-feat-t3")
        assert row["status"] == "awaiting_input"
        assert "Outcome verdict: moved" in row["analysis"]
        outcome = worker.store.outcome_for("watch-feat-t3")
        assert outcome["verdict"] == "moved"
        assert outcome["observed"] == 120.0 and outcome["baseline"] == 80.0
        assert outcome["feature_id"] == "feat-t3"
        inputs = json.loads(outcome["verdict_inputs"])
        assert inputs["target_numeric"] == 100.0

    def test_unavailable_provider_still_parks_unmeasured(self, worker, monkeypatch):
        provider = ScriptedProvider(result={"status": "unavailable", "series": [],
                                            "total": None, "detail": "none"})
        monkeypatch.setattr(worker, "_analytics_provider_for", lambda job: provider)
        _watch(worker, "watch-feat-t4", deadline_offset=-60, target="100")
        asyncio.run(worker._watch_pass())
        row = worker.store.get("watch-feat-t4")
        assert row["status"] == "awaiting_input"
        assert worker.store.outcome_for("watch-feat-t4")["verdict"] == "unmeasured"

    def test_crash_safe_ordering_db_before_clickup(self, worker, monkeypatch):
        """Amendment 13: the outcomes row + the awaiting_input CAS commit BEFORE
        any ClickUp call; every ClickUp step is best-effort after it."""
        seen = {}

        class OrderCU:
            enabled = True

            def __init__(self, store):
                self.store = store

            async def comments(self, task_id):
                return []

            async def comment(self, task_id, text):
                row = self.store.get("watch-feat-t5")
                seen.setdefault("status_at_comment", row["status"])
                seen.setdefault("outcome_at_comment",
                                worker.store.outcome_for("watch-feat-t5"))

            async def set_status(self, task_id, state):
                seen.setdefault("set_status", state)

            async def set_assignee(self, task_id, user_id):
                seen.setdefault("assignee", str(user_id))

        worker.clickup = OrderCU(worker.store)
        provider = ScriptedProvider()
        monkeypatch.setattr(worker, "_analytics_provider_for", lambda job: provider)
        _watch(worker, "watch-feat-t5", deadline_offset=-60, target=">= 10",
               task_id="cu9")
        asyncio.run(worker._watch_pass())
        assert seen["status_at_comment"] == "awaiting_input"  # CAS committed first
        assert seen["outcome_at_comment"] is not None          # ledger row first
        assert seen["set_status"] == "awaiting_input"
        assert seen["assignee"] == "111"

    def test_lost_cas_aborts_silently(self, worker, monkeypatch):
        """A human raced the finish (/skip flipped the row) — no comment, no park."""
        posted = []

        class CU:
            enabled = True

            async def comments(self, task_id):
                # simulate the race INSIDE the finish: skip lands mid-flight
                worker.store.set_status("watch-feat-t6", "skipped")
                return []

            async def comment(self, task_id, text):
                posted.append(text)

            async def set_status(self, task_id, state):
                posted.append(state)

        worker.clickup = CU()
        provider = ScriptedProvider()
        monkeypatch.setattr(worker, "_analytics_provider_for", lambda job: provider)
        _watch(worker, "watch-feat-t6", deadline_offset=-60, target=">= 10",
               task_id="cu9")
        asyncio.run(worker._watch_pass())
        assert worker.store.get("watch-feat-t6")["status"] == "skipped"
        assert posted == []  # nothing after the lost CAS

    def test_redo_refinish_keeps_original_baseline(self, worker, monkeypatch):
        """Amendment 11: the baseline persists from the FIRST finish — a redo
        window's re-finish must not re-query one ending at the refreshed start."""
        provider = ScriptedProvider(
            result={"status": "ok", "series": [], "total": 95.0, "detail": ""},
            by_end={"baseline": {"status": "ok", "series": [], "total": 80.0,
                                 "detail": ""}})
        monkeypatch.setattr(worker, "_analytics_provider_for", lambda job: provider)
        _watch(worker, "watch-feat-t7", deadline_offset=-60, target=">= 100")
        asyncio.run(worker._watch_pass())
        assert worker.store.outcome_for("watch-feat-t7")["baseline"] == 80.0
        baseline_calls = len([c for c in provider.calls if c["end"] is not None])
        assert baseline_calls == 1
        # /redo re-arms; second finish must reuse the stored baseline
        asyncio.run(worker.answer_job("watch-feat-t7", "redo", "3", via="dashboard:m"))
        worker.store.set_fields("watch-feat-t7", watch_deadline=time.time() - 30)
        asyncio.run(worker._watch_pass())
        assert worker.store.outcome_for("watch-feat-t7")["baseline"] == 80.0
        assert len([c for c in provider.calls if c["end"] is not None]) == baseline_calls

    def test_watch_disabled_is_a_noop(self, worker, monkeypatch):
        worker.settings.watch_enabled = False
        called = []
        monkeypatch.setattr(worker, "_analytics_provider_for",
                            lambda job: called.append(job))
        _watch(worker, "watch-feat-t8", deadline_offset=-60)
        asyncio.run(worker._watch_pass())
        assert called == []


class TestWatchNeverRunsClaude:
    def test_process_returns_watch_to_the_loop(self, worker, monkeypatch):
        """Blocker 1: ANY path that queues a watch job hits the fail-closed
        branch — no Claude run, ever; the row goes back to 'watching'."""
        _watch(worker, "watch-feat-p1")
        worker.store.set_status("watch-feat-p1", "queued")  # simulated bug/stale wakeup

        async def boom(*a, **k):
            raise AssertionError("a watch job must never reach a Claude path")

        monkeypatch.setattr(worker, "_process_task", boom)
        monkeypatch.setattr(worker, "_process_sentry", boom)
        monkeypatch.setattr(worker.engine, "run_stage", boom)
        job = worker.store.get("watch-feat-p1")
        asyncio.run(worker._process(job))
        assert worker.store.get("watch-feat-p1")["status"] == "watching"


class TestAnswerWatch:
    def _parked(self, worker, job_id="watch-feat-a1", **kw):
        job = _watch(worker, job_id, deadline_offset=-60, **kw)
        worker.store.outcome_add(job_id, job_id.removeprefix("watch-"), None,
                                 metric="m", verdict="flat", observed=5.0)
        worker.store.set_status(job_id, "awaiting_input")
        return job_id

    def test_proceed_records_learning_and_closes(self, worker):
        worker.settings.outcome_memory_prs = False
        job_id = self._parked(worker)
        status = asyncio.run(worker.answer_job(
            job_id, "proceed", "smaller batches win", via="dashboard:manish"))
        assert status == "done"
        row = worker.store.get(job_id)
        assert row["status"] == "done"
        outcome = worker.store.outcome_for(job_id)
        assert outcome["learning"] == "smaller batches win"
        assert outcome["decided_by"] == "dashboard:manish"
        assert outcome["decided_at"] is not None
        log = worker.store.guidance_for(job_id)
        assert log[-1]["action"] == "proceed"

    def test_redo_rearms_the_watch_with_new_window(self, worker):
        job_id = self._parked(worker)
        before = worker.store.get(job_id)
        status = asyncio.run(worker.answer_job(job_id, "redo", "7 look again",
                                               via="clickup:jane"))
        assert status == "watching"
        row = worker.store.get(job_id)
        assert row["status"] == "watching"
        assert row["metric_window_days"] == 7
        assert row["watch_started_at"] > (before["watch_started_at"] or 0)
        assert abs(row["watch_deadline"] - (row["watch_started_at"] + 7 * 86400)) < 5
        assert worker.store.guidance_for(job_id)[-1]["text"] == "look again"
        # the finished outcome row survives the redo
        assert worker.store.outcome_for(job_id) is not None

    def test_redo_without_days_keeps_the_original_window(self, worker):
        job_id = self._parked(worker, window=14)
        asyncio.run(worker.answer_job(job_id, "redo", "", via="dashboard:m"))
        assert worker.store.get(job_id)["metric_window_days"] == 14

    def test_redo_out_of_range_days_is_refused_with_reason(self, worker):
        """Amendment 7: 366+ days must be refused, never silently accepted."""
        job_id = self._parked(worker)
        with pytest.raises(ValueError) as e:
            asyncio.run(worker.answer_job(job_id, "redo", "999", via="dashboard:m"))
        assert "1–365" in str(e.value)
        assert worker.store.get(job_id)["status"] == "awaiting_input"

    def test_skip_closes_with_decider_and_empty_learning(self, worker):
        job_id = self._parked(worker)
        status = asyncio.run(worker.answer_job(job_id, "skip", "", via="clickup:jane"))
        assert status == "skipped"
        outcome = worker.store.outcome_for(job_id)
        assert outcome["decided_by"] == "clickup:jane"
        assert outcome["learning"] == ""
        assert outcome["verdict"] == "flat"  # the verdict stands

    def test_cas_loser_raises_gate_conflict(self, worker):
        worker.settings.outcome_memory_prs = False
        job_id = self._parked(worker)
        asyncio.run(worker.answer_job(job_id, "proceed", "a", via="dashboard:m"))
        with pytest.raises(GateConflict):
            asyncio.run(worker.answer_job(job_id, "proceed", "b", via="clickup:x"))

    def test_replayed_finish_after_proceed_keeps_the_learning(self, worker, monkeypatch):
        """Amendment 4: a crash-replayed _finish_watch after /proceed must never
        clobber learning/decided_* (and cannot re-park a done job)."""
        worker.settings.outcome_memory_prs = False
        job_id = self._parked(worker)
        asyncio.run(worker.answer_job(job_id, "proceed", "the learning",
                                      via="dashboard:m"))
        provider = ScriptedProvider()
        monkeypatch.setattr(worker, "_analytics_provider_for", lambda job: provider)
        job = worker.store.get(job_id)
        asyncio.run(worker._finish_watch(job, provider))
        row = worker.store.get(job_id)
        assert row["status"] == "done"  # the CAS from 'watching' lost — no re-park
        outcome = worker.store.outcome_for(job_id)
        assert outcome["learning"] == "the learning"
        assert outcome["decided_by"] == "dashboard:m"

    def test_proceed_schedules_the_memory_task_when_enabled(self, worker, monkeypatch):
        job_id = self._parked(worker)
        worker.settings.outcome_memory_prs = True
        ran = []

        async def fake_task(job):
            ran.append(job["issue_id"])

        monkeypatch.setattr(worker, "_outcome_memory_task", fake_task)

        async def go():
            await worker.answer_job(job_id, "proceed", "x", via="dashboard:m")
            await asyncio.sleep(0)  # let the background task run
            await asyncio.gather(*worker._bg_tasks) if worker._bg_tasks else None

        asyncio.run(go())
        assert ran == [job_id]

    def test_redo_still_refused_for_v1_kinds(self, worker):
        worker.intake_task("task-wr", title="T", project="web", request="r")
        worker.store.set_status("task-wr", "awaiting_input")
        with pytest.raises(ValueError):
            asyncio.run(worker.answer_job("task-wr", "redo", "", via="dashboard"))

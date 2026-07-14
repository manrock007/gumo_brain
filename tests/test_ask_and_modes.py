import asyncio

import pytest

from app.engine import parse_stage_output
from app.worker import GateConflict


class TestParseAsk:
    def test_ask_marker(self):
        marker, payload, _ = parse_stage_output(
            "committed what I have\nSTAGE_ASK:\nTax path ambiguity.\n## Questions\n1. per-line or per-total?"
        )
        assert marker == "ask"
        assert "per-line or per-total" in payload

    def test_last_marker_wins_across_all_three(self):
        text = "STAGE_ASK:\nearly q\n## Questions\n1. x?\n...\nSTAGE_DONE:\nfinal\n## Questions\n1. approve?"
        assert parse_stage_output(text)[0] == "done"
        text2 = "STAGE_DONE:\nfirst\n## Questions\n1. ok?\n...\nSTAGE_ASK:\nreal question\n## Questions\n1. y?"
        assert parse_stage_output(text2)[0] == "ask"


def _park_ask(worker, job_id="feat-a1", stage=5):
    worker.intake_feature(job_id, title="F", project="web", request="r")
    worker.store.set_fields(
        job_id, stage=stage, stage_attempts=1, parked_head="h" * 12,
        gate_kind="ask", resume_session_id="sess-123", resume_stage=stage,
        resume_attempt=1, resume_head="h" * 12, question="1. per-line or per-total?",
    )
    worker.store.set_status(job_id, "awaiting_input")
    return job_id


class TestAskAnswerTransition:
    def test_proceed_is_answer_keeps_stage(self, worker):
        job_id = _park_ask(worker, stage=5)
        status = asyncio.run(worker.answer_job(job_id, "proceed", "per-total, banker's rounding", via="dashboard"))
        row = worker.store.get(job_id)
        assert status == "queued"
        assert row["stage"] == 5                      # NOT advanced
        assert row["stage_attempts"] == 1             # NOT reset
        assert row["gate_kind"] == "ask"              # consumed by the run, not the answer
        assert row["resume_answer"] == "per-total, banker's rounding"
        assert row["resume_session_id"] == "sess-123"
        assert worker.store.guidance_for(job_id)[-1]["action"] == "answer"

    def test_ask_never_hits_terminal_branch(self, worker):
        # defensive: even if an ask somehow parked at stage 9, the answer resumes
        job_id = _park_ask(worker, stage=9)
        status = asyncio.run(worker.answer_job(job_id, "proceed", "answer", via="clickup"))
        row = worker.store.get(job_id)
        assert status == "queued"
        assert row["status"] == "queued"
        assert row["stage"] == 9

    def test_redo_at_ask_gate_discards_resume(self, worker):
        job_id = _park_ask(worker, stage=5)
        worker.store.set_fields(job_id, ask_count=2)
        status = asyncio.run(worker.answer_job(job_id, "redo", "restart with clearer plan", via="dashboard"))
        row = worker.store.get(job_id)
        assert status == "queued"
        assert row["gate_kind"] == ""
        assert row["resume_session_id"] == ""
        assert row["resume_answer"] == ""
        assert row["ask_count"] == 0
        assert row["pending_redo_stage"] == 5

    def test_skip_at_ask_gate(self, worker):
        job_id = _park_ask(worker)
        assert asyncio.run(worker.answer_job(job_id, "skip", "", via="dashboard")) == "skipped"

    def test_double_answer_conflicts(self, worker):
        job_id = _park_ask(worker)
        asyncio.run(worker.answer_job(job_id, "proceed", "a", via="dashboard"))
        with pytest.raises((GateConflict, ValueError)):
            asyncio.run(worker.answer_job(job_id, "proceed", "b", via="clickup"))

    def test_normal_proceed_resets_ask_count(self, worker):
        worker.intake_feature("feat-a2", title="F", project="web", request="r")
        worker.store.set_fields("feat-a2", stage=5, stage_attempts=1, ask_count=2)
        worker.store.set_status("feat-a2", "awaiting_input")
        asyncio.run(worker.answer_job("feat-a2", "proceed", "", via="dashboard"))
        row = worker.store.get("feat-a2")
        assert row["stage"] == 6
        assert row["ask_count"] == 0


class TestResumeIntended:
    def test_resume_intended(self, worker):
        job_id = _park_ask(worker, stage=5)
        worker.store.set_fields(job_id, resume_answer="use per-total")
        job = worker.store.get(job_id)
        assert worker.engine._resume_intended(job, 5) is True
        assert worker.engine._resume_intended(job, 6) is False

    def test_not_intended_without_answer(self, worker):
        job_id = _park_ask(worker, stage=5)
        job = worker.store.get(job_id)
        assert worker.engine._resume_intended(job, 5) is False


class TestAutoAdvance:
    def _job(self, worker, job_id="feat-m1", mode="light", stage=7, **fields):
        worker.intake_feature(job_id, title="F", project="web", request="r", gate_mode=mode)
        if fields or stage:
            worker.store.set_fields(job_id, stage=stage, **fields)
        return worker.store.get(job_id)

    BOILER = "results table...\n## Questions\n1. Approve and continue to the next stage?"
    REAL_Q = "## Questions\n1. Should deletes cascade?\n2. Approve?"

    def test_full_mode_never_auto_advances(self, worker):
        job = self._job(worker, mode="full")
        assert worker.engine._auto_advance_ok(job, 7, self.BOILER, None, []) is False

    def test_light_mode_boilerplate_advances(self, worker):
        job = self._job(worker, "feat-m2")
        assert worker.engine._auto_advance_ok(job, 7, self.BOILER, None, []) is True

    def test_checkpoint_stages_always_park(self, worker):
        job = self._job(worker, "feat-m3", stage=3)
        assert worker.engine._auto_advance_ok(job, 3, self.BOILER, None, []) is False
        assert worker.engine._auto_advance_ok(job, 9, self.BOILER, None, []) is False

    def test_real_questions_park(self, worker):
        job = self._job(worker, "feat-m4")
        assert worker.engine._auto_advance_ok(job, 7, self.REAL_Q, None, []) is False

    def test_p5_without_pr_parks(self, worker):
        job = self._job(worker, "feat-m5", stage=5)
        assert worker.engine._auto_advance_ok(job, 5, self.BOILER, None, []) is False
        assert worker.engine._auto_advance_ok(job, 5, self.BOILER, "https://github.com/x/y/pull/1", []) is True

    def test_first_run_after_redo_parks(self, worker):
        job = self._job(worker, "feat-m6")
        worker.store.guidance_add("feat-m6", 7, "redo", "tighter", "dashboard")
        assert worker.engine._auto_advance_ok(job, 7, self.BOILER, None, []) is False

    def test_conflicted_artifacts_park(self, worker):
        job = self._job(worker, "feat-m7")
        assert worker.engine._auto_advance_ok(job, 7, self.BOILER, None, ["P4-plan.md"]) is False

    def test_mirror_down_parks(self, worker):
        job = self._job(worker, "feat-m8", mirror_ok=0)
        assert worker.engine._auto_advance_ok(job, 7, self.BOILER, None, []) is False


class TestChatDistillation:
    def test_proceed_records_last_engine_answer(self, worker):
        worker.intake_feature("feat-d1", title="F", project="web", request="r")
        worker.store.set_fields("feat-d1", stage=3, stage_attempts=1)
        worker.store.set_status("feat-d1", "awaiting_input")
        worker.store.chat_add("feat-d1", 3, 1, "human", "why B?")
        worker.store.chat_add("feat-d1", 3, 1, "engine", "B avoids a migration because ...")
        asyncio.run(worker.answer_job("feat-d1", "proceed", "go with B", via="dashboard"))
        actions = [g["action"] for g in worker.store.guidance_for("feat-d1")]
        assert "chat" in actions and "proceed" in actions

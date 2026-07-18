import asyncio

import pytest

from app.worker import GateConflict, GateForbidden, extract_questions


class TestExtractQuestions:
    def test_extracts_questions_section(self):
        analysis = (
            "## Root cause\nfoo\n\n## Fix strategy\nbar\n\n"
            "## Questions\n1. Field on model A, or new model B?\n2. Flag the old behaviour?"
        )
        q = extract_questions(analysis)
        assert q.startswith("1. Field on model A")
        assert "Flag the old behaviour?" in q
        assert "Fix strategy" not in q

    def test_takes_last_questions_heading(self):
        # stage payloads may embed earlier artifacts that also have a Questions section
        text = "## Questions\n1. old?\n\n## Design\nstuff\n\n## Questions\n1. new?"
        assert extract_questions(text) == "1. new?"

    def test_stops_at_next_heading(self):
        q = extract_questions("## Open Questions\n1. x?\n\n## Appendix\nstuff")
        assert q == "1. x?"

    def test_falls_back_to_tail_without_heading(self):
        assert extract_questions("no headings at all") == "no headings at all"

    def test_empty_input(self):
        assert extract_questions("") == ""


class TestIntakeGuards:
    def test_task_intake_queues_then_guards_active(self, worker):
        assert "queued" in worker.intake_task("task-1", title="T", project="web", request="r")
        assert "already" in worker.intake_task("task-1", title="T", project="web", request="r")

    def test_task_intake_guards_awaiting_and_pr(self, worker):
        worker.intake_task("task-2", title="T", project="web", request="r")
        worker.store.set_status("task-2", "awaiting_input")
        assert "awaiting" in worker.intake_task("task-2", title="T", project="web", request="r")
        worker.store.set_status("task-2", "pr_opened", pr_url="http://pr")
        assert "PR" in worker.intake_task("task-2", title="T", project="web", request="r")

    def test_completed_task_can_be_resubmitted(self, worker):
        worker.intake_task("task-3", title="T", project="web", request="r")
        worker.store.set_status("task-3", "no_fix")
        assert "queued" in worker.intake_task("task-3", title="T", project="web", request="r")

    def test_feature_intake_and_guards(self, worker):
        d = worker.intake_feature("feat-1", title="F", project="web", request="build it")
        assert "queued" in d and "P0" in d
        row = worker.store.get("feat-1")
        assert row["kind"] == "feature" and row["stage"] == 0

        assert "already" in worker.intake_feature("feat-1", title="F", project="web", request="r")
        worker.store.set_status("feat-1", "awaiting_input")
        assert "gate" in worker.intake_feature("feat-1", title="F", project="web", request="r")
        worker.store.set_status("feat-1", "error")
        assert "redo" in worker.intake_feature("feat-1", title="F", project="web", request="r")
        worker.store.set_status("feat-1", "pr_opened", pr_url="http://pr")
        assert "shipped" in worker.intake_feature("feat-1", title="F", project="web", request="r")

    def test_skipped_feature_restarts_fresh(self, worker):
        worker.intake_feature("feat-2", title="F", project="web", request="r")
        worker.store.set_fields("feat-2", stage=5)
        worker.store.artifact_set("feat-2", "P1-prd.md", subtask_id="x", synced_hash="h")
        worker.store.guidance_add("feat-2", 3, "redo", "old dead-pipeline note", "dashboard")
        worker.store.set_status("feat-2", "skipped")

        assert "queued" in worker.intake_feature("feat-2", title="F", project="web", request="r")
        row = worker.store.get("feat-2")
        assert row["stage"] == 0
        assert worker.store.artifacts_for("feat-2") == []
        # the dead pipeline's guidance must NOT leak in as binding corrections
        assert worker.store.guidance_for("feat-2") == []

    def test_priority_ordering(self, worker):
        worker.intake_task("task-p", title="T", project="web", request="r")     # PRIO_HUMAN
        worker.intake("123", source="manual", forced=True)                       # PRIO_SENTRY
        worker.intake("456", source="sweep")                                     # PRIO_SWEEP
        order = []
        while not worker.queue.empty():
            order.append(worker.queue.get_nowait()[2])
        assert order == ["123", "task-p", "456"]


class TestAnswerV1:
    def _park(self, worker, job_id="task-9"):
        worker.intake_task(job_id, title="T", project="web", request="r")
        worker.store.set_status(job_id, "awaiting_input")
        worker.store.set_fields(job_id, question="1. A or B?")
        return job_id

    def test_proceed_advances_to_phase_2(self, worker):
        job_id = self._park(worker)
        status = asyncio.run(worker.answer_job(job_id, "proceed", "Use B", via="dashboard"))
        row = worker.store.get(job_id)
        assert status == "queued"
        assert row["phase"] == 2
        assert row["guidance"] == "Use B"
        assert row["question"] == ""

    def test_empty_answer_defaults_guidance(self, worker):
        job_id = self._park(worker)
        asyncio.run(worker.answer_job(job_id, "proceed", "", via="dashboard"))
        assert worker.store.get(job_id)["guidance"] == "Proceed as you proposed."

    def test_skip(self, worker):
        job_id = self._park(worker)
        status = asyncio.run(worker.answer_job(job_id, "skip", "", via="clickup"))
        assert status == "skipped"
        assert worker.store.get(job_id)["status"] == "skipped"

    def test_redo_invalid_for_tasks(self, worker):
        job_id = self._park(worker)
        with pytest.raises(ValueError):
            asyncio.run(worker.answer_job(job_id, "redo", "", via="dashboard"))

    def test_rejects_wrong_state_and_unknown(self, worker):
        job_id = self._park(worker)
        worker.store.set_status(job_id, "running")
        with pytest.raises(ValueError):
            asyncio.run(worker.answer_job(job_id, "proceed", "x", via="dashboard"))
        with pytest.raises(KeyError):
            asyncio.run(worker.answer_job("nope", "proceed", "x", via="dashboard"))


class TestAnswerFeature:
    def _park_feature(self, worker, job_id="feat-9", stage=3):
        worker.intake_feature(job_id, title="F", project="web", request="r")
        worker.store.set_fields(job_id, stage=stage, parked_head="abc123def456")
        worker.store.set_status(job_id, "awaiting_input")
        return job_id

    def test_proceed_advances_stage(self, worker):
        job_id = self._park_feature(worker, stage=3)
        status = asyncio.run(worker.answer_job(job_id, "proceed", "option B", via="dashboard"))
        row = worker.store.get(job_id)
        assert status == "queued"
        assert row["stage"] == 4
        assert row["stage_attempts"] == 0
        log = worker.store.guidance_for(job_id)
        assert log[-1]["action"] == "proceed"
        assert log[-1]["stage"] == 3
        assert log[-1]["artifact_sha"] == "abc123def456"

    def test_proceed_at_p9_terminates(self, worker):
        job_id = self._park_feature(worker, stage=9)
        worker.store.set_fields(job_id, pr_url="https://github.com/x/y/pull/1")
        status = asyncio.run(worker.answer_job(job_id, "proceed", "", via="clickup"))
        assert status == "pr_opened"
        assert worker.store.get(job_id)["status"] == "pr_opened"

    def test_redo_same_stage_sets_pending_redo_flag(self, worker):
        job_id = self._park_feature(worker, stage=5)
        status = asyncio.run(worker.answer_job(job_id, "redo", "tighter tests", via="dashboard"))
        row = worker.store.get(job_id)
        assert status == "queued"
        assert row["stage"] == 5
        # the flag tells the engine to rewind THIS stage to baseline (vs a mere re-advance)
        assert row["pending_redo_stage"] == 5
        assert worker.store.guidance_for(job_id)[-1]["text"] == "tighter tests"

    def test_redo_targets_earlier_stage(self, worker):
        job_id = self._park_feature(worker, stage=5)
        asyncio.run(worker.answer_job(job_id, "redo", "P3 wrong data model", via="clickup"))
        row = worker.store.get(job_id)
        assert row["stage"] == 3
        assert row["pending_redo_stage"] == 3
        entry = worker.store.guidance_for(job_id)[-1]
        assert entry["stage"] == 3
        assert entry["text"] == "wrong data model"

    def test_proceed_does_not_set_redo_flag(self, worker):
        # re-advancing through a stage must NOT trigger the baseline rewind
        job_id = self._park_feature(worker, stage=3)
        asyncio.run(worker.answer_job(job_id, "proceed", "", via="dashboard"))
        assert worker.store.get(job_id)["pending_redo_stage"] is None

    def test_redo_cannot_target_future_stage(self, worker):
        job_id = self._park_feature(worker, stage=2)
        with pytest.raises(ValueError):
            asyncio.run(worker.answer_job(job_id, "redo", "P7 nope", via="dashboard"))

    def test_redo_allowed_from_error(self, worker):
        job_id = self._park_feature(worker, stage=4)
        worker.store.set_status(job_id, "error")
        status = asyncio.run(worker.answer_job(job_id, "redo", "", via="dashboard"))
        assert status == "queued"
        assert worker.store.get(job_id)["stage"] == 4

    def test_cas_conflict_on_double_answer(self, worker):
        job_id = self._park_feature(worker, stage=3)
        asyncio.run(worker.answer_job(job_id, "proceed", "", via="dashboard"))
        with pytest.raises((GateConflict, ValueError)):
            asyncio.run(worker.answer_job(job_id, "proceed", "", via="clickup"))

    def test_skip_aborts_pipeline(self, worker):
        job_id = self._park_feature(worker, stage=6)
        status = asyncio.run(worker.answer_job(job_id, "skip", "not now", via="dashboard"))
        assert status == "skipped"


class TestRoleExclusiveGates:
    """Epic A3: _answer_feature refuses non-owners at the single choke point,
    before any CAS — and stays INERT when no explicit DRIs are recorded."""

    def _park(self, worker, job_id="feat-rx1", stage=5, status="awaiting_input", **fields):
        worker.intake_feature(job_id, title="F", project="web", request="r", **fields)
        worker.store.set_fields(job_id, stage=stage, parked_head="abc123")
        worker.store.set_status(job_id, status)
        return job_id

    def _user(self, worker, username, cu_id="", role="member"):
        worker.store.user_create(username, "hash", role=role)
        if cu_id:
            worker.store.user_set(username, clickup_user_id=cu_id)
        return worker.store.user_get(username)

    def test_gate_forbidden_is_not_a_value_error(self):
        # main.py maps ValueError->409 and _scan_verbs swallows it with a
        # generic reply — a ValueError subclass would silently break the 403
        assert not issubclass(GateForbidden, ValueError)
        assert issubclass(GateForbidden, Exception)

    def test_no_dris_any_actor_proceeds(self, worker):
        """Solo-mode regression lock: enforcement is N/A without DRIs."""
        job_id = self._park(worker)
        anyone = self._user(worker, "someone")
        status = asyncio.run(worker.answer_job(job_id, "proceed", "", via="dashboard:someone",
                                               actor=anyone))
        assert status == "queued"

    def test_legacy_owner_only_never_enforces(self, worker):
        """Blocker 1 / upgrade shape: a pre-existing job with a numeric legacy
        `owner`, no DRI columns and zero mappings must answer normally — no
        override, no 403 — or an upgrade bricks every in-flight gate."""
        job_id = self._park(worker, "feat-rx2")
        worker.store.set_fields(job_id, owner="4242", founder_dri="", dev_dri="")
        member = self._user(worker, "olduser")
        status = asyncio.run(worker.answer_job(job_id, "proceed", "", via="dashboard:olduser",
                                               actor=member))
        assert status == "queued"
        assert worker.store.gate_events_for(job_id) == []  # no override needed/recorded

    def test_wrong_role_is_refused(self, worker):
        dev = self._user(worker, "dev1", cu_id="222")
        job_id = self._park(worker, "feat-rx3", stage=0,  # P0 = founder gate
                            founder_dri="111", dev_dri="222")
        with pytest.raises(GateForbidden) as e:
            asyncio.run(worker.answer_job(job_id, "proceed", "", via="dashboard:dev1",
                                          actor=dev))
        assert "founder gate" in str(e.value)
        assert worker.store.get(job_id)["status"] == "awaiting_input"

    def test_owner_proceeds_by_clickup_id_mapping(self, worker):
        dev = self._user(worker, "dev1", cu_id="222")
        job_id = self._park(worker, "feat-rx4", stage=5, dev_dri="222")
        status = asyncio.run(worker.answer_job(job_id, "proceed", "", via="dashboard:dev1",
                                               actor=dev))
        assert status == "queued"

    def test_owner_proceeds_by_username_dri(self, worker):
        jane = self._user(worker, "jane")
        job_id = self._park(worker, "feat-rx5", stage=0, founder_dri="jane")
        status = asyncio.run(worker.answer_job(job_id, "proceed", "", via="dashboard:jane",
                                               actor=jane))
        assert status == "queued"

    def test_admin_override_proceeds_and_is_audited(self, worker):
        admin = self._user(worker, "boss", role="admin")
        job_id = self._park(worker, "feat-rx6", stage=5, dev_dri="222")
        # without override even the admin is refused
        with pytest.raises(GateForbidden):
            asyncio.run(worker.answer_job(job_id, "proceed", "", via="dashboard:boss",
                                          actor=admin))
        status = asyncio.run(worker.answer_job(job_id, "proceed", "", via="dashboard:boss",
                                               actor=admin, override=True))
        assert status == "queued"
        events = worker.store.gate_events_for(job_id)
        assert [e["kind"] for e in events] == ["admin_override"]
        assert events[0]["actor"] == "dashboard:boss"
        assert "dev gate" in events[0]["detail"]

    def test_member_override_flag_is_ignored(self, worker):
        member = self._user(worker, "sneak")
        job_id = self._park(worker, "feat-rx7", stage=5, dev_dri="222")
        with pytest.raises(GateForbidden):
            asyncio.run(worker.answer_job(job_id, "proceed", "", via="dashboard:sneak",
                                          actor=member, override=True))

    def test_lost_cas_leaves_no_override_audit_row(self, worker):
        """Blocker 2: an override that never took effect (lost the race) must
        not leave an audit record claiming it did."""
        admin = self._user(worker, "boss", role="admin")
        dev = self._user(worker, "dev1", cu_id="222")
        job_id = self._park(worker, "feat-rx8", stage=5, dev_dri="222")
        asyncio.run(worker.answer_job(job_id, "proceed", "", via="dashboard:dev1", actor=dev))
        with pytest.raises((GateConflict, ValueError)):
            asyncio.run(worker.answer_job(job_id, "proceed", "", via="dashboard:boss",
                                          actor=admin, override=True))
        assert worker.store.gate_events_for(job_id) == []

    def test_two_overrides_both_audited(self, worker):
        """Blocker 2b: audit rows must never be dedupe-dropped — two overrides
        in the same second are two records."""
        admin = self._user(worker, "boss", role="admin")
        job_id = self._park(worker, "feat-rx9", stage=5, dev_dri="222")
        asyncio.run(worker.answer_job(job_id, "redo", "again", via="dashboard:boss",
                                      actor=admin, override=True))
        worker.store.set_status(job_id, "awaiting_input")
        asyncio.run(worker.answer_job(job_id, "redo", "again2", via="dashboard:boss",
                                      actor=admin, override=True))
        events = worker.store.gate_events_for(job_id)
        assert [e["kind"] for e in events] == ["admin_override", "admin_override"]

    def test_ask_gate_is_enforced_too(self, worker):
        dev = self._user(worker, "dev1", cu_id="222")
        jane = self._user(worker, "jane", cu_id="111")
        job_id = self._park(worker, "feat-rx10", stage=0, founder_dri="111", dev_dri="222")
        worker.store.set_fields(job_id, gate_kind="ask", resume_session_id="s1",
                                resume_stage=0, resume_answer="")
        with pytest.raises(GateForbidden):
            asyncio.run(worker.answer_job(job_id, "proceed", "answer", via="dashboard:dev1",
                                          actor=dev))
        status = asyncio.run(worker.answer_job(job_id, "proceed", "answer",
                                               via="dashboard:jane", actor=jane))
        assert status == "queued"

    def test_redo_from_error_is_enforced(self, worker):
        dev = self._user(worker, "dev1", cu_id="222")
        job_id = self._park(worker, "feat-rx11", stage=0, status="error",
                            founder_dri="111", dev_dri="222")
        with pytest.raises(GateForbidden):
            asyncio.run(worker.answer_job(job_id, "redo", "", via="dashboard:dev1",
                                          actor=dev))

    def test_other_dri_covers_an_empty_slot(self, worker):
        """A dev-owned stage with only a founder DRI recorded: the founder is
        the effective owner (fallback), and enforcement holds."""
        jane = self._user(worker, "jane", cu_id="111")
        rando = self._user(worker, "rando")
        job_id = self._park(worker, "feat-rx12", stage=5, founder_dri="111")
        with pytest.raises(GateForbidden):
            asyncio.run(worker.answer_job(job_id, "proceed", "", via="dashboard:rando",
                                          actor=rando))
        assert asyncio.run(worker.answer_job(job_id, "proceed", "", via="dashboard:jane",
                                             actor=jane)) == "queued"


class TestSentryLaneGate:
    def test_sweep_exits_when_sentry_unconfigured(self, worker):
        """sweep_forever must return immediately (not sleep/loop) on an
        instance without a configured Sentry integration."""
        worker.settings.sweep_enabled = True
        assert not worker.settings.sentry_enabled
        asyncio.run(worker.sweep_forever())  # returns immediately or the test hangs

    def test_sweep_exits_when_disabled(self, worker):
        worker.settings.sweep_enabled = False
        asyncio.run(worker.sweep_forever())


class TestV1BranchPersistence:
    def test_task_branch_uses_prefix_and_persists(self, worker, monkeypatch, tmp_path):
        import app.worker as worker_mod
        from app.fixer import FixResult

        worker.intake_task("task-br1", title="T", project="web", request="r")
        seen = {}

        async def fake_ws(settings, target, branch, keep_branch=False, workspace_root=None):
            seen["branch"] = branch
            return str(tmp_path)

        async def fake_run(settings, target, workspace, prompt, on_event=None):
            return FixResult("no_fix", detail="nothing to do")

        monkeypatch.setattr(worker_mod, "prepare_workspace", fake_ws)
        monkeypatch.setattr(worker_mod, "run_claude", fake_run)
        asyncio.run(worker._process_task(worker.store.get("task-br1")))
        assert seen["branch"] == "ctrlloop/task-br1"
        assert worker.store.get("task-br1")["branch"] == "ctrlloop/task-br1"

    def test_task_stored_branch_wins(self, worker, monkeypatch, tmp_path):
        import app.worker as worker_mod
        from app.fixer import FixResult

        worker.intake_task("task-br2", title="T", project="web", request="r")
        worker.store.set_fields("task-br2", branch="brain/task-br2")  # backfilled row
        seen = {}

        async def fake_ws(settings, target, branch, keep_branch=False, workspace_root=None):
            seen["branch"] = branch
            return str(tmp_path)

        async def fake_run(settings, target, workspace, prompt, on_event=None):
            return FixResult("no_fix", detail="nothing to do")

        monkeypatch.setattr(worker_mod, "prepare_workspace", fake_ws)
        monkeypatch.setattr(worker_mod, "run_claude", fake_run)
        asyncio.run(worker._process_task(worker.store.get("task-br2")))
        assert seen["branch"] == "brain/task-br2"
        assert worker.store.get("task-br2")["branch"] == "brain/task-br2"

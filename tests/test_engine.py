import asyncio

from app.engine import BUILD_GROUP_RE, extract_questions_last, parse_stage_output


class TestParseStageOutput:
    def test_done_with_payload(self):
        marker, payload, pr = parse_stage_output(
            "I explored the repo.\n\nSTAGE_DONE:\n## Understanding\nstuff\n\n## Questions\n1. ok?"
        )
        assert marker == "done"
        assert payload.startswith("## Understanding")
        assert pr is None

    def test_fail(self):
        marker, payload, _ = parse_stage_output("STAGE_FAIL: request too vague, need the target screen")
        assert marker == "fail"
        assert "too vague" in payload

    def test_unparsed_fails_closed(self):
        marker, payload, _ = parse_stage_output("I did lots of stuff and forgot the marker")
        assert marker == "unparsed"

    def test_last_marker_wins(self):
        text = "STAGE_FAIL: first thoughts\n...more work...\nSTAGE_DONE:\nfinal answer\n## Questions\n1. ok?"
        marker, payload, _ = parse_stage_output(text)
        assert marker == "done"
        assert payload.startswith("final answer")

    def test_marker_must_be_line_start(self):
        marker, _, _ = parse_stage_output("as discussed STAGE_DONE: is the protocol")
        assert marker == "unparsed"

    def test_bare_pr_url_never_counts(self):
        marker, _, pr = parse_stage_output(
            "see https://github.com/x/y/pull/9 for context\nSTAGE_DONE:\nsummary\n## Questions\n1. ok?"
        )
        assert marker == "done"
        assert pr is None

    def test_standalone_pr_line_is_recorded(self):
        text = ("work done\nPR_URL: https://github.com/acme/web/pull/42\n"
                "STAGE_DONE:\nbuilt group 1\n## Questions\n1. approve?")
        marker, _, pr = parse_stage_output(text)
        assert marker == "done"
        assert pr == "https://github.com/acme/web/pull/42"

    def test_pr_line_tolerates_backticks_and_bullets(self):
        # models often wrap the line; the regex must still find it
        for line in (
            "PR_URL: `https://github.com/x/y/pull/7`",
            "- PR_URL: https://github.com/x/y/pull/7",
            "> PR_URL:  https://github.com/x/y/pull/7",
        ):
            _, _, pr = parse_stage_output(f"{line}\nSTAGE_DONE:\nok\n## Questions\n1. ok?")
            assert pr == "https://github.com/x/y/pull/7", line

    def test_empty(self):
        assert parse_stage_output("")[0] == "unparsed"


class TestBuildGroupRegex:
    def test_matches_h2_and_h3_and_case(self):
        assert len(BUILD_GROUP_RE.findall("## Build group 1\n...\n## Build group 2\n")) == 2
        assert len(BUILD_GROUP_RE.findall("### build group A\n#### Build Group B\n")) == 2

    def test_single_group(self):
        assert len(BUILD_GROUP_RE.findall("## Build group 1\nonly one")) == 1


class TestExtractQuestionsLast:
    def test_last_heading_wins(self):
        text = "## Questions\n1. embedded from P1?\n\n## Design\n...\n\n## Questions\n1. real gate question?"
        assert extract_questions_last(text) == "1. real gate question?"

    def test_fallback_tail(self):
        assert extract_questions_last("just prose") == "just prose"


class TestBranchResolution:
    """Epic 0.2: stored branch wins; new jobs get the configured prefix and
    persist it before first use."""

    def test_new_feature_gets_configured_prefix_and_persists(self, worker):
        worker.intake_feature("feat-b1", title="F", project="web", request="r")
        job = worker.store.get("feat-b1")
        assert job["branch"] == ""
        branch = worker.engine._branch(job)
        assert branch == "ctrlloop/feat-feat-b1"  # job ids already carry feat-
        assert worker.store.get("feat-b1")["branch"] == branch

    def test_stored_branch_wins_over_prefix(self, worker):
        worker.intake_feature("feat-b2", title="F", project="web", request="r")
        worker.store.set_fields("feat-b2", branch="brain/feat-feat-b2")  # backfilled row
        job = worker.store.get("feat-b2")
        assert worker.engine._branch(job) == "brain/feat-feat-b2"
        assert worker.store.get("feat-b2")["branch"] == "brain/feat-feat-b2"

    def test_custom_prefix_is_used(self, worker):
        worker.settings.branch_prefix = "team-x"
        worker.intake_feature("feat-b3", title="F", project="web", request="r")
        assert worker.engine._branch(worker.store.get("feat-b3")) == "team-x/feat-feat-b3"

    def test_memory_job_branch(self, worker):
        worker.intake_memory("web")
        job = worker.store.get("mem-web")
        assert worker.engine._branch(job) == "ctrlloop/memory-web"
        assert worker.store.get("mem-web")["branch"] == "ctrlloop/memory-web"


class ParkFakeCU:
    enabled = True

    def __init__(self):
        self.assigned = []  # (task_id, user_id)
        self.posted = []    # (task_id, text)

    async def comments(self, task_id):
        return []

    async def comment(self, task_id, text):
        self.posted.append((task_id, text))

    async def set_status(self, task_id, state):
        pass

    async def set_assignee(self, task_id, user_id):
        self.assigned.append((task_id, str(user_id)))

    async def field_set(self, task_id, field, value):
        return True

    async def field_append(self, task_id, field, line):
        return True


class TestParkOwnership:
    """Epic A3: at gate park the OWNING DRI is assigned and named — founder at
    P0/P1/P9, dev at P2–P8 — with the legacy `owner` column as an
    assignment-only fallback (no ownership claim in the comment)."""

    def _park(self, worker, tmp_path, job_id, stage, **fields):
        worker.intake_feature(job_id, title="F", project="web", request="r",
                              clickup_task_id="cu1", **fields)
        worker.store.set_fields(job_id, stage=stage, stage_attempts=1)
        run_id = worker.store.stage_run_open(job_id, stage, 1)
        job = worker.store.get(job_id)
        target = worker.settings.repo_for_project("web")
        asyncio.run(worker.engine._park(
            job, stage, run_id, str(tmp_path), target, "",
            "done\n## Questions\n1. Approve?", None))
        return worker.store.get(job_id)

    def test_founder_assigned_at_p0(self, worker, tmp_path):
        fake = ParkFakeCU()
        worker.engine.clickup = fake
        row = self._park(worker, tmp_path, "feat-pk1", 0,
                         founder_dri="111", dev_dri="222")
        assert row["status"] == "awaiting_input"
        assert fake.assigned == [("cu1", "111")]
        gate = next(t for _, t in fake.posted if "Gate: P0" in t)
        assert "Owned by ClickUp user 111 (founder gate)" in gate
        assert "Only ClickUp user 111" in gate and "admin override" in gate

    def test_dev_assigned_at_p5(self, worker, tmp_path):
        fake = ParkFakeCU()
        worker.engine.clickup = fake
        self._park(worker, tmp_path, "feat-pk2", 5, founder_dri="111", dev_dri="222")
        assert fake.assigned == [("cu1", "222")]
        gate = next(t for _, t in fake.posted if "Gate: P5" in t)
        assert "Owned by ClickUp user 222 (dev gate)" in gate

    def test_legacy_owner_fallback_assigns_without_claim(self, worker, tmp_path):
        fake = ParkFakeCU()
        worker.engine.clickup = fake
        worker.intake_feature("feat-pk3", title="F", project="web", request="r",
                              clickup_task_id="cu1")
        worker.store.set_fields("feat-pk3", owner="4242")  # pre-upgrade shape
        worker.store.set_fields("feat-pk3", stage=3, stage_attempts=1)
        run_id = worker.store.stage_run_open("feat-pk3", 3, 1)
        target = worker.settings.repo_for_project("web")
        asyncio.run(worker.engine._park(
            worker.store.get("feat-pk3"), 3, run_id, str(tmp_path), target, "",
            "done\n## Questions\n1. Approve?", None))
        assert fake.assigned == [("cu1", "4242")]  # assignment unchanged...
        gate = next(t for _, t in fake.posted if "Gate: P3" in t)
        assert "Owned by" not in gate  # ...but no enforcement claim

    def test_no_owner_no_assignment(self, worker, tmp_path):
        fake = ParkFakeCU()
        worker.engine.clickup = fake
        self._park(worker, tmp_path, "feat-pk4", 2)
        assert fake.assigned == []


class TestSuccessMetricHeading:
    def test_matches_h2_h3_and_case(self):
        from app.engine import SUCCESS_METRIC_HEADING_RE as RE
        assert RE.search("## Success metric\ntext")
        assert RE.search("### Success Metric\ntext")
        assert RE.search("#### success metric (proposed)\n")
        assert RE.search("intro\n\n## Success metric\n")

    def test_mid_line_and_prose_do_not_match(self):
        from app.engine import SUCCESS_METRIC_HEADING_RE as RE
        assert not RE.search("the success metric is signups")
        assert not RE.search("see ## Success metric above")
        assert not RE.search("## Success\nmetric later")


class TestMissingMetricGate:
    """Epic B1 fail-closed check: a P0/P1 STAGE_DONE without '## Success
    metric' closes the run 'missing_metric' and parks flagged — after the
    artifact commit + checkpoint, before auto-advance (light mode included)."""

    PAYLOAD_NO_METRIC = ("table\nSTAGE_DONE:\n## Understanding\nstuff\n"
                         "## Questions\n1. Approve and continue to the next stage?")
    PAYLOAD_WITH_METRIC = ("table\nSTAGE_DONE:\n## Understanding\nstuff\n"
                           "## Success metric\nsignups +10% in 14 days\n"
                           "## Questions\n1. Approve and continue to the next stage?")

    def _after_run(self, worker, monkeypatch, tmp_path, job_id, stage, text,
                   gate_mode="full"):
        eng = worker.engine
        worker.intake_feature(job_id, title="F", project="web", request="r",
                              gate_mode=gate_mode)
        worker.store.set_fields(job_id, stage=stage, stage_attempts=1)
        job = worker.store.get(job_id)
        run_id = worker.store.stage_run_open(job_id, stage, 1, None)

        class Raw:
            status = "ok"
            meta = {}

        Raw.text = text

        async def truthy(*a, **k):
            return True

        async def empty_list(*a, **k):
            return []

        async def anoop(*a, **k):
            return None

        monkeypatch.setattr(eng, "_checkpoint", truthy)
        monkeypatch.setattr(eng.sync, "push", empty_list)
        monkeypatch.setattr(eng.sync, "commit_file", truthy)
        monkeypatch.setattr(eng, "_comment", anoop)
        target = worker.settings.repo_for_project("web")
        asyncio.run(eng._after_run(job, stage, run_id, target, "b",
                                   str(tmp_path), Raw(), "base"))
        return worker.store.get(job_id), worker.store.stage_runs_for(job_id)[-1]

    def test_p0_without_section_parks_missing_metric(self, worker, monkeypatch, tmp_path):
        row, run = self._after_run(worker, monkeypatch, tmp_path,
                                   "feat-mm1", 0, self.PAYLOAD_NO_METRIC)
        assert run["result_status"] == "missing_metric"  # never closed 'done'
        assert row["status"] == "awaiting_input"
        assert row["stage"] == 0  # did NOT advance
        assert "MISSING '## Success metric'" in row["evidence"]

    def test_p1_without_section_parks_in_light_mode_too(self, worker, monkeypatch, tmp_path):
        row, run = self._after_run(worker, monkeypatch, tmp_path,
                                   "feat-mm2", 1, self.PAYLOAD_NO_METRIC,
                                   gate_mode="light")
        assert run["result_status"] == "missing_metric"
        assert row["status"] == "awaiting_input" and row["stage"] == 1

    def test_p0_with_section_parks_normally(self, worker, monkeypatch, tmp_path):
        row, run = self._after_run(worker, monkeypatch, tmp_path,
                                   "feat-mm3", 0, self.PAYLOAD_WITH_METRIC)
        assert run["result_status"] == "done"
        assert row["status"] == "awaiting_input"
        assert "MISSING" not in (row["evidence"] or "")

    def test_p2_without_section_is_unaffected(self, worker, monkeypatch, tmp_path):
        row, run = self._after_run(worker, monkeypatch, tmp_path,
                                   "feat-mm4", 2, self.PAYLOAD_NO_METRIC)
        assert run["result_status"] == "done"

    def test_light_mode_p2_still_auto_advances(self, worker, monkeypatch, tmp_path):
        row, run = self._after_run(worker, monkeypatch, tmp_path,
                                   "feat-mm5", 2, self.PAYLOAD_NO_METRIC,
                                   gate_mode="light")
        assert run["result_status"] == "done"
        assert row["stage"] == 3  # auto-advanced — the check is P0/P1-scoped


class TestHarvestMetricLines:
    def _job(self, worker, job_id="feat-hv1", **fields):
        worker.intake_feature(job_id, title="F", project="web", request="r",
                              **fields)
        return worker.store.get(job_id)

    def test_fills_only_empty_fields_intake_wins(self, worker):
        job = self._job(worker, success_metric="human metric",
                        metric_target="human target")
        worker.engine.harvest_metric_lines(job, (
            "SUCCESS_METRIC: engine metric\nMETRIC_TARGET: engine target\n"
            "METRIC_WINDOW_DAYS: 21\nMETRIC_EVENT: signup_done\n"))
        row = worker.store.get("feat-hv1")
        assert row["success_metric"] == "human metric"   # intake wins
        assert row["metric_target"] == "human target"
        assert row["metric_window_days"] == 21           # was NULL — filled
        assert row["metric_event"] == "signup_done"      # engine-owned

    def test_fills_empty_fields_and_event_always_updates(self, worker):
        job = self._job(worker, "feat-hv2")
        worker.engine.harvest_metric_lines(job, "SUCCESS_METRIC: signups\n"
                                                "METRIC_EVENT: old_event\n")
        row = worker.store.get("feat-hv2")
        assert row["success_metric"] == "signups"
        worker.engine.harvest_metric_lines(row, "METRIC_EVENT: new_event\n")
        assert worker.store.get("feat-hv2")["metric_event"] == "new_event"

    def test_out_of_range_window_is_ignored(self, worker):
        job = self._job(worker, "feat-hv3")
        worker.engine.harvest_metric_lines(job, "METRIC_WINDOW_DAYS: 999\n")
        assert worker.store.get("feat-hv3")["metric_window_days"] is None

    def test_runs_with_clickup_field_sync_disabled(self, worker):
        worker.settings.clickup_field_sync_enabled = False
        job = self._job(worker, "feat-hv4")
        worker.engine.harvest_metric_lines(job, "SUCCESS_METRIC: still lands\n")
        assert worker.store.get("feat-hv4")["success_metric"] == "still lands"

    def test_non_feature_jobs_are_ignored(self, worker):
        worker.store.insert("task-hv", source="manual", kind="task", project="web")
        worker.engine.harvest_metric_lines(worker.store.get("task-hv"),
                                           "SUCCESS_METRIC: nope\n")
        assert (worker.store.get("task-hv")["success_metric"] or "") == ""

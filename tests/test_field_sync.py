"""The gumo-speed conveyor mirror: the engine reflects its state onto the
original workflow's ClickUp custom fields — the `Stage` dropdown (the board),
the per-repo PR url fields, the `Decisions` append log, and the `Dashboard`
deep link. Best-effort display only; the engine's store stays the record."""

import asyncio


class FakeFieldClickUp:
    enabled = True

    def __init__(self):
        self.sets = []      # (task_id, field, value)
        self.appends = []   # (task_id, field, line)

    async def field_set(self, task_id, field, value):
        self.sets.append((task_id, field, value))
        return True

    async def field_append(self, task_id, field, line):
        self.appends.append((task_id, field, line))
        return True

    async def comment(self, task_id, text):
        pass

    async def set_status(self, task_id, state):
        pass


def _feature(worker, job_id, stage=3, project="web"):
    worker.intake_feature(job_id, title="F", project=project, request="r",
                          clickup_task_id="cu1", clickup_task_url="https://cu/x")
    worker.store.set_fields(job_id, stage=stage, stage_attempts=1)
    return worker.store.get(job_id)


class TestStageFieldSync:
    def test_stage_maps_to_board_column(self, worker):
        fake = FakeFieldClickUp()
        worker.engine.clickup = fake
        job = _feature(worker, "feat-fs1", stage=3)
        asyncio.run(worker.engine.sync_stage_field(job, "3"))
        assert ("cu1", "Stage", "Contract") in fake.sets

    def test_build_stage_resolves_per_repo(self, worker):
        fake = FakeFieldClickUp()
        worker.engine.clickup = fake
        # project 'web' -> manrock007/gumowebclient -> Frontend - Web
        job = _feature(worker, "feat-fs2", stage=5)
        asyncio.run(worker.engine.sync_stage_field(job, "5"))
        assert ("cu1", "Stage", "Frontend - Web") in fake.sets

    def test_shipped_and_merged_columns(self, worker):
        fake = FakeFieldClickUp()
        worker.engine.clickup = fake
        job = _feature(worker, "feat-fs3", stage=9)
        asyncio.run(worker.engine.sync_stage_field(job, "shipped"))
        asyncio.run(worker.engine.sync_stage_field(job, "merged"))
        assert ("cu1", "Stage", "Dogfood") in fake.sets
        assert ("cu1", "Stage", "Complete") in fake.sets

    def test_non_feature_and_disabled_are_noops(self, worker):
        fake = FakeFieldClickUp()
        worker.engine.clickup = fake
        worker.store.insert("sen-fs1", source="webhook", kind="sentry", project="web")
        worker.store.set_fields("sen-fs1", clickup_task_id="cu2")
        asyncio.run(worker.engine.sync_stage_field(worker.store.get("sen-fs1"), "3"))
        assert fake.sets == []
        worker.settings.clickup_field_sync_enabled = False
        job = _feature(worker, "feat-fs4")
        asyncio.run(worker.engine.sync_stage_field(job, "3"))
        assert fake.sets == []


class TestPrAndDashboardFieldSync:
    def test_pr_field_by_repo(self, worker):
        fake = FakeFieldClickUp()
        worker.engine.clickup = fake
        _feature(worker, "feat-fs5")
        asyncio.run(worker.engine.sync_pr_field(
            "feat-fs5", "manrock007/gumoserver", "https://github.com/manrock007/gumoserver/pull/9"))
        assert ("cu1", "Backend PR", "https://github.com/manrock007/gumoserver/pull/9") in fake.sets

    def test_unknown_repo_is_a_noop(self, worker):
        fake = FakeFieldClickUp()
        worker.engine.clickup = fake
        _feature(worker, "feat-fs6")
        asyncio.run(worker.engine.sync_pr_field(
            "feat-fs6", "other/repo", "https://github.com/other/repo/pull/9"))
        assert fake.sets == []

    def test_record_prs_fills_the_field(self, worker, monkeypatch):
        fake = FakeFieldClickUp()
        worker.engine.clickup = fake
        _feature(worker, "feat-fs7", project="web")
        url = "https://github.com/manrock007/gumowebclient/pull/12"
        asyncio.run(worker.engine.record_prs("feat-fs7", [url], kickoff=False))
        assert ("cu1", "Web PR", url) in fake.sets

    def test_dashboard_deep_link(self, worker):
        fake = FakeFieldClickUp()
        worker.engine.clickup = fake
        job = _feature(worker, "feat-fs8")
        asyncio.run(worker.engine.sync_dashboard_field(job))
        assert any(f == "Dashboard" and v.endswith("#/job/feat-fs8")
                   for _, f, v in fake.sets)


class TestDocAndReportFieldSync:
    def test_doc_fields_point_at_artifact_subtasks(self, worker):
        fake = FakeFieldClickUp()
        worker.engine.clickup = fake
        job = _feature(worker, "feat-fs11")
        worker.store.artifact_set("feat-fs11", "P1-prd.md", subtask_id="sub1")
        worker.store.artifact_set("feat-fs11", "P3-design.md", subtask_id="sub3")
        asyncio.run(worker.engine.sync_doc_fields(job))
        assert ("cu1", "PRD Doc", "https://app.clickup.com/t/sub1") in fake.sets
        assert ("cu1", "Contract Doc", "https://app.clickup.com/t/sub3") in fake.sets
        assert any(f == "PRD Folder" and ".gumo/features/feat-fs11" in v
                   for _, f, v in fake.sets)

    def test_friction_lines_feed_the_improvements_log(self, worker):
        fake = FakeFieldClickUp()
        worker.engine.clickup = fake
        job = _feature(worker, "feat-fs12", stage=7)
        text = ("did the work\n"
                "FRICTION: test plan lacked seed data shapes · include fixtures in P4\n"
                "FRICTION: repo test cmd missing · configure test_cmd\n"
                "STAGE_DONE: all green\n## Questions\n1. Approve?")
        asyncio.run(worker.engine.sync_run_report_fields(job, 7, text))
        lines = [line for _, f, line in fake.appends if f == "Gumo Workflow Improvements"]
        assert len(lines) == 2
        assert lines[0].startswith("P7 Test (engine) · ")
        assert "seed data shapes" in lines[0]

    def test_p9_flag_and_metric_fields(self, worker):
        fake = FakeFieldClickUp()
        worker.engine.clickup = fake
        job = _feature(worker, "feat-fs13", stage=9)
        text = ("FLAG_NAME: import_health_api\nSUCCESS_METRIC: ops MTTR on import incidents\n"
                "STAGE_DONE: shipped\n## Questions\n1. Approve?")
        asyncio.run(worker.engine.sync_run_report_fields(job, 9, text))
        assert ("cu1", "Flag name", "import_health_api") in fake.sets
        assert ("cu1", "Success metric", "ops MTTR on import incidents") in fake.sets

    def test_clean_run_appends_nothing(self, worker):
        fake = FakeFieldClickUp()
        worker.engine.clickup = fake
        job = _feature(worker, "feat-fs14")
        asyncio.run(worker.engine.sync_run_report_fields(
            job, 3, "STAGE_DONE: fine\n## Questions\n1. Approve?"))
        assert fake.appends == [] and fake.sets == []


class TestDriAdoption:
    def test_feature_adoption_reads_dev_dri(self, worker):
        from tests.test_intake_clickup import FakeClickUp, _cu_task

        fake = FakeClickUp(tasks=[_cu_task("td1", "[feature] flagged feature")],
                           bodies={"td1": "project: web\nbuild it"})
        fake.fields = {"td1": {"assigned dev dri": [{"id": 4242, "username": "dev"}]}}
        worker.clickup = fake
        asyncio.run(worker._poll_intake())
        assert worker.store.get("feat-td1")["owner"] == "4242"


class TestDecisionsFieldSync:
    def test_proceed_guidance_appends(self, worker):
        fake = FakeFieldClickUp()
        worker.clickup = fake
        worker.engine.clickup = fake
        _feature(worker, "feat-fs9", stage=4)
        worker.store.set_status("feat-fs9", "awaiting_input")
        asyncio.run(worker.answer_job("feat-fs9", "proceed", "lock option B", via="clickup"))
        assert any(f == "Decisions" and "P4: lock option B" in line
                   for _, f, line in fake.appends)

    def test_default_approval_does_not_pollute_decisions(self, worker):
        fake = FakeFieldClickUp()
        worker.clickup = fake
        worker.engine.clickup = fake
        _feature(worker, "feat-fs10", stage=4)
        worker.store.set_status("feat-fs10", "awaiting_input")
        asyncio.run(worker.answer_job("feat-fs10", "proceed", "", via="clickup"))
        assert fake.appends == []

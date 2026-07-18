"""Epic A5: the gate-SLA escalation ladder. Keyed idempotently on the gated
run's stage_runs id (crash-safe, re-arms per attempt, survives re-intake);
INERT for jobs without explicit DRIs — a solo install gets zero new noise."""

import asyncio
import time

from tests.test_attribution import FakeCU


class FakeWS:
    """Minimal workspace service: one row served for every job."""

    def __init__(self, row=None):
        self.row = row
        self.nudges = []

    def for_job(self, job):
        return self.row

    def for_project(self, project):
        return None  # intake's workspace stamping is not under test here

    async def notify_gate(self, job, text):
        self.nudges.append(text)


def _gate(worker, job_id="feat-sla1", stage=5, hours_ago=25.0, **fields):
    worker.intake_feature(job_id, title=job_id, project="web", request="r",
                          clickup_task_id="cu1", **fields)
    worker.store.set_fields(job_id, stage=stage)
    worker.store.set_status(job_id, "awaiting_input")
    rid = worker.store.stage_run_open(job_id, stage, 1)
    with worker.store._conn() as c:
        c.execute("UPDATE stage_runs SET gate_posted_at = ? WHERE id = ?",
                  (time.time() - hours_ago * 3600, rid))
    return rid


def _kinds(worker, job_id):
    return [e["kind"] for e in worker.store.gate_events_for(job_id)]


class TestSlaLadder:
    def test_step1_fires_once(self, worker):
        worker.clickup = FakeCU()
        rid = _gate(worker, hours_ago=25, dev_dri="222")
        asyncio.run(worker._sla_once())
        assert _kinds(worker, "feat-sla1") == ["sla_nudge"]
        assert worker.store.gate_events_for("feat-sla1")[0]["ref"] == f"run{rid}-step1"
        nudges = [t for _, t in worker.clickup.posted if "⏰" in t]
        assert len(nudges) == 1 and "P5" in nudges[0] and "ClickUp user 222" in nudges[0]
        assert worker.clickup.assigned == [("cu1", "222")]  # owner re-assigned
        # re-run: a no-op — no second comment, no second event
        asyncio.run(worker._sla_once())
        assert _kinds(worker, "feat-sla1") == ["sla_nudge"]
        assert len([t for _, t in worker.clickup.posted if "⏰" in t]) == 1

    def test_below_threshold_is_silent(self, worker):
        worker.clickup = FakeCU()
        _gate(worker, hours_ago=23, dev_dri="222")
        asyncio.run(worker._sla_once())
        assert _kinds(worker, "feat-sla1") == []
        assert worker.clickup.posted == []

    def test_step2_notifies_the_other_dri(self, worker):
        worker.store.user_create("founda", "hash")
        worker.store.user_set("founda", clickup_user_id="111")
        worker.clickup = FakeCU()
        _gate(worker, hours_ago=37, founder_dri="111", dev_dri="222")
        asyncio.run(worker._sla_once())
        assert _kinds(worker, "feat-sla1") == ["sla_nudge", "sla_second_dri"]
        second = [t for _, t in worker.clickup.posted if "founda" in t]
        assert len(second) == 1
        assert "can't answer this dev gate" in second[0]

    def test_step2_skipped_without_other_dri(self, worker):
        worker.clickup = FakeCU()
        _gate(worker, hours_ago=37, dev_dri="222")  # no founder recorded
        asyncio.run(worker._sla_once())
        assert _kinds(worker, "feat-sla1") == ["sla_nudge"]

    def test_step3_records_the_standup_flag(self, worker):
        worker.clickup = FakeCU()
        rid = _gate(worker, hours_ago=49, founder_dri="111", dev_dri="222")
        asyncio.run(worker._sla_once())
        assert _kinds(worker, "feat-sla1") == ["sla_nudge", "sla_second_dri",
                                              "sla_standup_flag"]
        flag = worker.store.gate_events_for("feat-sla1")[-1]
        assert flag["ref"] == f"run{rid}-step3"
        assert "exhausted" in flag["detail"]

    def test_zero_sla_disables(self, worker):
        worker.settings.gate_sla_hours = 0
        worker.clickup = FakeCU()
        _gate(worker, hours_ago=100, dev_dri="222")
        asyncio.run(worker._sla_once())
        assert _kinds(worker, "feat-sla1") == []

    def test_workspace_override_beats_instance(self, worker):
        worker.workspaces = FakeWS({"gate_sla_hours": 1, "require_attributed_answers": "auto",
                                    "stage_role_map": ""})
        worker.clickup = FakeCU()
        _gate(worker, hours_ago=2, dev_dri="222")  # 2h > 1h workspace SLA (< 24h default)
        asyncio.run(worker._sla_once())
        assert _kinds(worker, "feat-sla1") == ["sla_nudge", "sla_standup_flag"]
        assert worker.workspaces.nudges  # Slack rode along

    def test_redo_new_attempt_rearms_the_ladder(self, worker):
        worker.clickup = FakeCU()
        _gate(worker, hours_ago=25, dev_dri="222")
        asyncio.run(worker._sla_once())
        # a redo parks a NEW gated run — fresh run id, fresh refs
        rid2 = worker.store.stage_run_open("feat-sla1", 5, 2)
        with worker.store._conn() as c:
            c.execute("UPDATE stage_runs SET gate_posted_at = ? WHERE id = ?",
                      (time.time() - 26 * 3600, rid2))
        asyncio.run(worker._sla_once())
        events = worker.store.gate_events_for("feat-sla1")
        assert [e["kind"] for e in events] == ["sla_nudge", "sla_nudge"]
        assert events[1]["ref"] == f"run{rid2}-step1"


class TestSlaInertia:
    def test_no_dri_job_is_untouched(self, worker):
        """Blocker 4: solo installs get NO new noise — no owner deref, no
        comments, no events."""
        worker.clickup = FakeCU()
        _gate(worker, hours_ago=100)
        asyncio.run(worker._sla_once())
        assert _kinds(worker, "feat-sla1") == []
        assert worker.clickup.posted == [] and worker.clickup.assigned == []

    def test_legacy_owner_only_job_is_untouched(self, worker):
        """Upgrade shape: a pre-existing job with only `owner` set never
        escalates (enforce=False) — no upgrade-day nudge burst."""
        worker.clickup = FakeCU()
        _gate(worker, hours_ago=100)
        worker.store.set_fields("feat-sla1", owner="4242")
        asyncio.run(worker._sla_once())
        assert _kinds(worker, "feat-sla1") == []
        assert worker.clickup.posted == []

    def test_v1_awaiting_jobs_untouched(self, worker):
        worker.clickup = FakeCU()
        worker.intake_task("task-sla1", title="T", project="web", request="r",
                           clickup_task_id="cu2")
        worker.store.set_status("task-sla1", "awaiting_input")
        asyncio.run(worker._sla_once())
        assert _kinds(worker, "task-sla1") == []
        assert worker.clickup.posted == []

    def test_gate_without_posted_run_is_skipped(self, worker):
        """error/timeout parks (or crash shapes) with no gate_posted_at row
        never fire — the ladder needs a real gated run to key on."""
        worker.clickup = FakeCU()
        worker.intake_feature("feat-sla2", title="F", project="web", request="r",
                              dev_dri="222")
        worker.store.set_fields("feat-sla2", stage=5)
        worker.store.set_status("feat-sla2", "awaiting_input")
        asyncio.run(worker._sla_once())
        assert _kinds(worker, "feat-sla2") == []

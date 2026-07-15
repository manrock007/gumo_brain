"""Boot-time crash recovery: a deploy restarts the container mid-run; at
startup any 'running' row is provably a corpse (the CLI subprocess died with
the old process) and must requeue immediately — not wait for the stale-run
reaper to surface it as an error an hour later."""

import asyncio


class TestStartupRecovery:
    def test_running_corpse_requeues_on_boot(self, worker):
        worker.intake_feature("feat-r1", title="F", project="web", request="r")
        worker.store.set_fields("feat-r1", stage=6, run_started_at=123.0)
        worker.store.set_status("feat-r1", "running")

        asyncio.run(worker._recover_interrupted())
        job = worker.store.get("feat-r1")
        assert job["status"] == "queued"
        assert "restart interrupted" in job["detail"]
        assert int(job["stage"]) == 6  # same stage re-runs; nothing rewound
        assert worker.queue.qsize() == 2  # intake enqueue + recovery enqueue

    def test_v1_running_corpse_requeues_too(self, worker):
        worker.store.insert("sen-r2", source="webhook", kind="sentry", project="web")
        worker.store.set_fields("sen-r2", phase=2, guidance="do it")
        worker.store.set_status("sen-r2", "running")

        asyncio.run(worker._recover_interrupted())
        job = worker.store.get("sen-r2")
        assert job["status"] == "queued"
        assert int(job["phase"]) == 2  # answered guidance survives the recovery

    def test_non_running_rows_untouched(self, worker):
        worker.intake_feature("feat-r3", title="F", project="web", request="r")
        worker.store.set_status("feat-r3", "awaiting_input")

        asyncio.run(worker._recover_interrupted())
        assert worker.store.get("feat-r3")["status"] == "awaiting_input"

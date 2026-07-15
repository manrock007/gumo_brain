"""ClickUp as an intake channel: '[fix] …', '[feature] …' and '[sentry <id>] …'
tickets in the autofix list are adopted and queued — the ClickUp mirror of the
dashboard's intake forms (built founder-mode: the dashboard needs a password,
the ClickUp workspace is already a trusted control surface)."""

import asyncio


class FakeClickUp:
    enabled = True

    def __init__(self, tasks=None, bodies=None):
        self.tasks = tasks or []            # list_tasks payloads
        self.bodies = bodies or {}          # task_id -> description
        self.comments_posted = []           # (task_id, text)

    async def list_tasks(self, list_id=None):
        return self.tasks

    async def get_task(self, task_id):
        return {"id": str(task_id), "name": "", "url": f"https://cu/{task_id}",
                "list_id": "L1", "description": self.bodies.get(task_id, "")}

    async def comment(self, task_id, text):
        self.comments_posted.append((task_id, text))

    async def set_status(self, task_id, state):
        pass

    async def load_statuses(self):
        pass


def _cu_task(task_id, name):
    return {"id": task_id, "name": name, "url": f"https://cu/{task_id}", "list_id": "L1"}


class TestClickUpIntake:
    def test_fix_ticket_adopts_as_task(self, worker):
        worker.clickup = FakeClickUp(
            tasks=[_cu_task("t1", "[fix] Send button dead on skipped items")],
            bodies={"t1": "project: web\n\nThe send button does nothing."})
        asyncio.run(worker._poll_intake())
        job = worker.store.get("task-t1")
        assert job is not None
        assert job["kind"] == "task"
        assert job["status"] in ("received", "queued")
        assert job["clickup_task_id"] == "t1"
        assert "send button does nothing" in (job["request"] or "").lower()
        assert "project:" not in (job["request"] or "").lower()
        assert any("adopted" in c[1] for c in worker.clickup.comments_posted)

    def test_bug_and_task_aliases_adopt_too(self, worker):
        worker.clickup = FakeClickUp(
            tasks=[_cu_task("t8", "[bug] crash"), _cu_task("t9", "[task] tweak copy")],
            bodies={"t8": "project: web\nboom", "t9": "project: web\nplease"})
        asyncio.run(worker._poll_intake())
        assert worker.store.get("task-t8")["kind"] == "task"
        assert worker.store.get("task-t9")["kind"] == "task"

    def test_feature_ticket_adopts_as_pipeline(self, worker):
        worker.clickup = FakeClickUp(
            tasks=[_cu_task("t2", "[feature] Export bookings as CSV")],
            bodies={"t2": "project: web\n\nAgents need CSV export of bookings."})
        asyncio.run(worker._poll_intake())
        job = worker.store.get("feat-t2")
        assert job is not None
        assert job["kind"] == "feature"
        assert int(job["stage"] or 0) == 0
        assert job["cu_list_id"] == "L1"

    def test_sentry_ticket_forces_the_issue(self, worker):
        worker.clickup = FakeClickUp(
            tasks=[_cu_task("t3", "[sentry 6613584091] 522 fetching place")])
        asyncio.run(worker._poll_intake())
        job = worker.store.get("6613584091")
        assert job is not None
        assert job["kind"] == "sentry"
        assert bool(job["forced"]) is True
        # the run must adopt THIS ticket instead of creating its own
        assert job["clickup_task_id"] == "t3"

    def test_sentry_without_id_rejects_once(self, worker):
        worker.clickup = FakeClickUp(tasks=[_cu_task("t4", "[sentry] no id here")])
        asyncio.run(worker._poll_intake())
        pinned = worker.store.get("cu-t4")
        assert pinned["status"] == "skipped"
        asyncio.run(worker._poll_intake())  # second scan: silent
        assert len(worker.clickup.comments_posted) == 1

    def test_sentry_short_code_resolves(self, worker):
        """Humans know issues as WEB-3Y, the API wants the group id — the scan
        resolves short codes through the Sentry client."""
        worker.clickup = FakeClickUp(tasks=[_cu_task("ts1", "[sentry WEB-3Y] null split")])

        async def resolve(short_id):
            assert short_id == "WEB-3Y"
            return "6650001234"

        worker.sentry.resolve_short_id = resolve
        asyncio.run(worker._poll_intake())
        job = worker.store.get("6650001234")
        assert job is not None and job["kind"] == "sentry"
        assert job["clickup_task_id"] == "ts1"

    def test_sentry_unknown_short_code_rejects(self, worker):
        worker.clickup = FakeClickUp(tasks=[_cu_task("ts2", "[sentry NOPE-1] ghost")])

        async def resolve(short_id):
            return ""  # definitively unknown (404)

        worker.sentry.resolve_short_id = resolve
        asyncio.run(worker._poll_intake())
        assert worker.store.get("cu-ts2")["status"] == "skipped"
        assert "did not resolve" in worker.store.get("cu-ts2")["detail"]

    def test_sentry_resolution_outage_retries(self, worker):
        """A transient Sentry failure must NOT pin the ticket — the next scan
        retries the resolution."""
        worker.clickup = FakeClickUp(tasks=[_cu_task("ts3", "[sentry WEB-3Y] flaky")])

        async def resolve(short_id):
            return None  # transient failure

        worker.sentry.resolve_short_id = resolve
        asyncio.run(worker._poll_intake())
        assert worker.store.get("cu-ts3") is None       # not pinned
        assert worker.clickup.comments_posted == []      # not commented

        async def resolve_ok(short_id):
            return "6650009999"

        worker.sentry.resolve_short_id = resolve_ok
        asyncio.run(worker._poll_intake())               # retry succeeds
        assert worker.store.get("6650009999") is not None

    def test_unmapped_project_rejects_once_with_reason(self, worker):
        worker.clickup = FakeClickUp(
            tasks=[_cu_task("t5", "[fix] broken thing")],
            bodies={"t5": "project: nope\nbody"})
        asyncio.run(worker._poll_intake())
        pinned = worker.store.get("cu-t5")
        assert pinned["status"] == "skipped"
        assert "no repo mapped" in pinned["detail"]
        asyncio.run(worker._poll_intake())
        assert len(worker.clickup.comments_posted) == 1

    def test_engine_created_and_plain_tickets_ignored(self, worker):
        worker.clickup = FakeClickUp(tasks=[
            _cu_task("t6", "[web] TypeError in checkout"),  # engine-created
            _cu_task("t7", "regular human note"),
        ])
        asyncio.run(worker._poll_intake())
        assert worker.store.get("task-t6") is None
        assert worker.store.get("cu-t6") is None
        assert worker.clickup.comments_posted == []

    def test_adopted_ticket_never_rescanned(self, worker):
        worker.clickup = FakeClickUp(
            tasks=[_cu_task("t1", "[fix] once only")],
            bodies={"t1": "project: web\nbody"})
        asyncio.run(worker._poll_intake())
        asyncio.run(worker._poll_intake())
        assert len(worker.clickup.comments_posted) == 1
        assert worker.store.get("task-t1")["attempts"] == 1

    def test_outage_is_a_noop(self, worker):
        class Down(FakeClickUp):
            async def list_tasks(self, list_id=None):
                return None

        worker.clickup = Down()
        asyncio.run(worker._poll_intake())  # must not raise

    def test_disabled_flag_is_a_noop(self, worker):
        worker.settings.clickup_intake_enabled = False
        worker.clickup = FakeClickUp(
            tasks=[_cu_task("t1", "[fix] nope")], bodies={"t1": "project: web\nx"})
        asyncio.run(worker._poll_intake())
        assert worker.store.get("task-t1") is None

    def test_bold_project_line_parses(self, worker):
        """ClickUp markdown often bolds labels — '**project:** web' must parse."""
        worker.clickup = FakeClickUp(
            tasks=[_cu_task("t10", "[fix] markdown body")],
            bodies={"t10": "**project:** web\n\ndetails"})
        asyncio.run(worker._poll_intake())
        assert worker.store.get("task-t10") is not None

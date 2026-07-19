"""Epic I2: the daily standup digest — exception-only, quiet days send
nothing, date-deduped, Slack only on a NEW insert, `since` floored at 24h."""

import asyncio
import time

import pytest

from app import digests, routines
from app.routines import RoutineContext


class FakeWorkspaces:
    def __init__(self):
        self.sent = []

    async def notify_text(self, ws, text):
        self.sent.append((ws["id"], text))


@pytest.fixture()
def ws(store, settings):
    with store._conn() as c:
        c.execute("INSERT INTO workspaces (slug, name, created_at, updated_at) "
                  "VALUES ('w1', 'W1', 1, 1)")
    routines.ensure_seeds(store, settings)
    return store.workspace_list()[0]


def _routine(store, ws, kind="standup_digest"):
    return next(r for r in store.routines_all()
                if r["workspace_id"] == ws["id"] and r["kind"] == kind)


def _ctx(settings, store, ws, fake_ws=None, now=None):
    return RoutineContext(settings=settings, store=store, worker=None,
                          workspaces=fake_ws, routine=_routine(store, ws),
                          now=now or time.time())


def _run(ctx):
    return asyncio.run(routines._handle_standup(ctx))


def _overdue_gate(store, ws, job_id="feat-sd1", hours_ago=30):
    store.feature_intake(job_id, title=job_id, project="demo", stage=5)
    store.set_fields(job_id, workspace_id=ws["id"])
    store.set_status(job_id, "awaiting_input")
    rid = store.stage_run_open(job_id, 5, 1)
    with store._conn() as c:
        c.execute("UPDATE stage_runs SET gate_posted_at = ? WHERE id = ?",
                  (time.time() - hours_ago * 3600, rid))


def test_quiet_day_sends_nothing(store, settings, ws):
    fake = FakeWorkspaces()
    status, detail, items = _run(_ctx(settings, store, ws, fake))
    assert status == "quiet" and items == 0
    assert store.inbox_items_open(None) == []
    assert fake.sent == []


def test_overdue_gate_section(store, settings, ws):
    _overdue_gate(store, ws)
    fake = FakeWorkspaces()
    status, _, items = _run(_ctx(settings, store, ws, fake))
    assert status == "ok" and items == 1
    notice = store.inbox_items_open(None)[0]
    assert notice["kind"] == "standup_digest"
    assert "Gates overdue" in notice["body"] and "feat-sd1" in notice["body"]
    # Slack sent exactly once, on the NEW insert
    assert len(fake.sent) == 1 and "Gates overdue" in fake.sent[0][1]


def test_each_section_triggers_independently(store, settings, ws):
    now = time.time()
    # blocked pipeline (error job)
    store.feature_intake("feat-err", title="broken thing", project="demo")
    store.set_fields("feat-err", workspace_id=ws["id"])
    store.set_status("feat-err", "error", detail="boom")
    # stalled PR
    store.feature_intake("feat-pr", title="pr thing", project="demo")
    store.set_fields("feat-pr", workspace_id=ws["id"])
    store.pr_add("feat-pr", "https://github.com/acme/demo/pull/9")
    store.pr_set("https://github.com/acme/demo/pull/9", state="stalled")
    # regressing watch (decrease target met? no: increase target missed)
    store.watch_insert("watch-feat-w", workspace_id=ws["id"],
                       title="watch: w", success_metric="signups",
                       metric_target="at least 100", metric_window_days=10,
                       watch_started_at=now - 6 * 86400,
                       watch_deadline=now + 4 * 86400)
    for day, obs in ((4, 10), (5, 12), (6, 13)):
        store.reading_add("watch-feat-w", "signups", "", observed=obs,
                          window_day=day, window_start=now - 6 * 86400)
    # autonomy change
    store.autonomy_event_add("level_change", workspace_id=ws["id"], project="demo",
                             stage=5, detail="P5 demo: level 1 → 2", actor="engine")
    # spend + budget
    store.set_fields("feat-err", workspace_id=ws["id"])
    rid = store.stage_run_open("feat-err", 0, 1)
    store.stage_run_close(rid, "done", cost_usd=50.0)
    with store._conn() as c:
        c.execute("UPDATE workspaces SET budget_monthly_usd = 10 WHERE id = ?",
                  (ws["id"],))
    ws_row = store.workspace_get(ws["id"])

    d = digests.build_standup(store, settings, ws_row, since=now - 86400, now=now)
    assert d is not None
    assert set(d["sections"]) == {"Blocked / stalled",
                                  "Watches trending off-goal",
                                  "Autonomy changes", "Budget"}
    blocked = "\n".join(d["sections"]["Blocked / stalled"])
    assert "broken thing" in blocked and "pull/9" in blocked
    assert "PACING OVER BUDGET" in d["sections"]["Budget"][0]
    assert "level 1 → 2" in "\n".join(d["sections"]["Autonomy changes"])


def test_date_dedupe_on_refire(store, settings, ws):
    _overdue_gate(store, ws)
    fake = FakeWorkspaces()
    assert _run(_ctx(settings, store, ws, fake))[0] == "ok"
    # a mid-day run-now re-fire: same date key → no second item, no second send
    status, _, items = _run(_ctx(settings, store, ws, fake))
    assert status == "ok" and items == 0
    assert len(store.inbox_items_open(None)) == 1
    assert len(fake.sent) == 1


def test_fresh_digest_expires_predecessor(store, settings, ws):
    _overdue_gate(store, ws)
    ctx = _ctx(settings, store, ws, FakeWorkspaces())
    _run(ctx)
    # simulate yesterday's digest still open
    with store._conn() as c:
        c.execute("UPDATE inbox_items SET dedupe_key = ?, created_at = ? "
                  "WHERE kind = 'standup_digest'",
                  (f"{ws['id']}:2000-01-01", time.time() - 86400))
    _run(ctx)
    rows = store.inbox_items_open(None)
    assert len(rows) == 1  # yesterday's flipped to expired by today's insert
    old = store.inbox_item_by_key("standup_digest", f"{ws['id']}:2000-01-01")
    assert old["status"] == "expired" and old["status_by"] == "engine"


def test_since_floored_at_24h(store, settings, ws):
    """Amendment 9: a first run (or long outage) must not replay history —
    events older than 24h stay out even with no prior success run."""
    now = time.time()
    store.feature_intake("feat-old", title="old flag", project="demo")
    store.set_fields("feat-old", workspace_id=ws["id"])
    store.gate_event_add("feat-old", "sla_standup_flag", ref="run1-step3",
                         stage=5, actor="engine", detail="ancient")
    with store._conn() as c:
        c.execute("UPDATE gate_events SET at = ?", (now - 3 * 86400,))
    d = digests.build_standup(store, settings, store.workspace_get(ws["id"]),
                              since=max(0, now - 86400), now=now)
    assert d is None  # the 3-day-old flag is outside the floored window
    # the handler floors even when the last success run is ancient
    routine = _routine(store, ws)
    run_id = store.routine_run_open(routine["id"], "standup_digest", ws["id"])
    store.routine_run_close(run_id, "ok")
    with store._conn() as c:
        c.execute("UPDATE routine_runs SET started_at = ?", (now - 10 * 86400,))
    status, _, _ = _run(_ctx(settings, store, ws, FakeWorkspaces(), now=now))
    assert status == "quiet"


def test_quiet_run_recorded_via_scheduler(store, settings, worker, ws):
    """End-to-end through the scheduler: the quiet handler records a 'quiet'
    routine_runs row."""
    routine = _routine(store, ws)
    sched = routines.RoutineScheduler(settings, store, worker, FakeWorkspaces())
    assert sched.request_run(routine["id"]) is True

    async def drive():
        sched._dispatch_due()
        for _ in range(10):
            await asyncio.sleep(0)
        if sched._tasks:
            await asyncio.gather(*list(sched._tasks), return_exceptions=True)

    asyncio.run(drive())
    runs = store.routine_runs_recent(routine["id"])
    assert runs and runs[0]["status"] == "quiet"

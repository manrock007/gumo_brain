"""Epic I1: the routine engine — schedule parsing, next_due, claim CAS,
seeding, builtin-loop generalization, run history, master-flag scoping."""

import asyncio
import sqlite3
import time
import types
from zoneinfo import ZoneInfo

import pytest

from app import routines
from app.routines import (
    RoutineScheduler,
    Schedule,
    builtin_default_schedule,
    next_due,
    parse_schedule,
)

UTC = ZoneInfo("UTC")


# ---------- schedule parsing ----------


def test_parse_every_and_floor():
    s = parse_schedule("every:3600")
    assert s.kind == "every" and s.seconds == 3600
    # never a hot loop: floors at 300
    assert parse_schedule("every:10").seconds == 300


def test_parse_daily_with_days_filter():
    s = parse_schedule("daily@09:30;days=mon,wed,fri")
    assert s.kind == "daily" and (s.hh, s.mm) == (9, 30)
    assert s.days == frozenset({0, 2, 4})
    # default: all days
    assert parse_schedule("daily@07:00").days == frozenset(range(7))


def test_parse_weekly():
    s = parse_schedule("weekly@mon 09:00")
    assert s.kind == "weekly" and s.days == frozenset({0})
    assert (s.hh, s.mm) == (9, 0)


@pytest.mark.parametrize("bad", [
    "", "hourly", "every:", "every:x", "every:-5", "daily@25:00",
    "daily@09:00;days=fun", "weekly@blursday 09:00", "weekly@mon", "daily@9",
])
def test_parse_bad_specs_raise(bad):
    with pytest.raises(ValueError):
        parse_schedule(bad)


# ---------- next_due ----------


def test_next_due_every():
    s = parse_schedule("every:3600")
    now = 1_000_000.0
    assert next_due(s, None, now, UTC) == now          # no history → due now
    assert next_due(s, now, now, UTC) == now + 3600


def test_next_due_daily_days_filter():
    s = parse_schedule("daily@09:00;days=mon")
    # 2026-07-13 is a Monday. Last run Monday 09:00 UTC → next is next Monday.
    mon_9 = 1783933200.0  # 2026-07-13T09:00:00Z
    nd = next_due(s, mon_9, mon_9 + 60, UTC)
    assert nd == pytest.approx(mon_9 + 7 * 86400)


def test_next_due_daily_no_history_waits_for_next_occurrence():
    s = parse_schedule("daily@09:00")
    mon_15 = 1783954800.0  # 2026-07-13T15:00:00Z — after today's slot
    nd = next_due(s, None, mon_15, UTC)
    assert nd > mon_15  # never fires the missed slot at boot
    assert nd == pytest.approx(1784019600.0)  # tomorrow 09:00Z


def test_next_due_dst_boundary():
    """US spring-forward (2026-03-08, America/New_York): the 09:00 local slot
    still resolves once per day through the transition."""
    tz = ZoneInfo("America/New_York")
    s = parse_schedule("daily@09:00")
    # Sat 2026-03-07 09:00 EST = 14:00 UTC
    sat_9 = 1772892000.0
    nd = next_due(s, sat_9, sat_9 + 60, tz)
    # Sun 2026-03-08 09:00 EDT = 13:00 UTC — 23 hours later, not 24
    assert nd - sat_9 == pytest.approx(23 * 3600)


# ---------- seeding ----------


def test_ensure_seeds_idempotent_and_operator_edits_survive(store, settings):
    with store._conn() as c:
        c.execute("INSERT INTO workspaces (slug, name, created_at, updated_at) "
                  "VALUES ('w1', 'W1', 1, 1)")
    ws_id = store.workspace_list()[0]["id"]
    routines.ensure_seeds(store, settings)
    rows = store.routines_all()
    builtins = [r for r in rows if r["workspace_id"] is None]
    ws_rows = [r for r in rows if r["workspace_id"] == ws_id]
    assert {r["kind"] for r in builtins} == set(routines.BUILTIN_KINDS)
    assert {r["kind"] for r in ws_rows} == set(routines.WORKSPACE_KINDS)
    assert all(r["schedule"] == "" for r in builtins)  # derive-from-settings
    # operator edit survives a re-seed
    standup = next(r for r in ws_rows if r["kind"] == "standup_digest")
    store.routine_set(standup["id"], schedule="daily@06:00", enabled=0)
    routines.ensure_seeds(store, settings)
    row = store.routine_get(standup["id"])
    assert row["schedule"] == "daily@06:00" and row["enabled"] == 0
    assert len(store.routines_all()) == len(rows)  # no duplicates


def test_boot_bump_stamps_sweep_and_janitor_only(store, settings):
    routines.ensure_seeds(store, settings)
    stamped = {r["kind"]: r["last_run_at"] for r in store.routines_all()
               if r["workspace_id"] is None}
    assert stamped["sweep"] is not None and stamped["janitor"] is not None
    assert stamped["reaper"] is None  # reaper stays due-immediately at boot


def test_seeding_anchors_daily_weekly_rows(store, settings):
    """daily/weekly next_due anchors on last_run_at — a NULL anchor re-slides
    to `now` at every tick, so the row could never come due. Seeding stamps
    it; every: rows keep NULL history (due immediately, by design)."""
    with store._conn() as c:
        c.execute("INSERT INTO workspaces (slug, name, created_at, updated_at) "
                  "VALUES ('w1', 'W1', 1, 1)")
    ws_id = store.workspace_list()[0]["id"]
    routines.ensure_seeds(store, settings)
    by_kind = {r["kind"]: r for r in store.routines_all()
               if r["workspace_id"] == ws_id}
    for kind in ("standup_digest", "memory_upkeep", "weekly_planning"):
        assert by_kind[kind]["last_run_at"] is not None, kind
    for kind in ("risk_scan", "proposal_scan"):
        assert by_kind[kind]["last_run_at"] is None, kind
    # upgrade path: a daily row seeded NULL by an older build backfills at boot
    store.routine_set(by_kind["standup_digest"]["id"], last_run_at=None)
    routines.ensure_seeds(store, settings)
    assert store.routine_get(
        by_kind["standup_digest"]["id"])["last_run_at"] is not None


# ---------- claim CAS ----------


def test_claim_cas_single_winner(store, settings):
    routines.ensure_seeds(store, settings)
    row = next(r for r in store.routines_all() if r["kind"] == "reaper")
    now = time.time()
    assert store.routine_claim(row["id"], row["last_run_at"], now) is True
    # a second claimer with the same stale prev loses
    assert store.routine_claim(row["id"], row["last_run_at"], now + 1) is False
    # disabled rows can't be claimed — except with the reaper escape
    store.routine_set(row["id"], enabled=0)
    assert store.routine_claim(row["id"], now, now + 2) is False
    assert store.routine_claim(row["id"], now, now + 2, ignore_disabled=True) is True


# ---------- the scheduler ----------


def _scheduler(settings, store, worker):
    return RoutineScheduler(settings, store, worker, None)


async def _drive(sched):
    sched._dispatch_due()
    for _ in range(10):
        await asyncio.sleep(0)
    if sched._tasks:
        await asyncio.gather(*list(sched._tasks), return_exceptions=True)


def test_builtin_handlers_fire_existing_bodies(store, settings, worker, monkeypatch):
    routines.ensure_seeds(store, settings)
    calls = []

    async def fake_sweep():
        calls.append("sweep")

    async def fake_reap(horizon):
        calls.append(("reap", horizon))

    monkeypatch.setattr(worker, "_sweep_once", fake_sweep)
    monkeypatch.setattr(worker, "_reap_once", fake_reap)
    monkeypatch.setattr(worker, "_janitor_once", lambda: calls.append("janitor"))
    settings.sweep_enabled = True
    settings.sentry_org = "acme"
    settings.sentry_auth_token = "tok"
    # make everything due NOW
    with store._conn() as c:
        c.execute("UPDATE routines SET last_run_at = NULL")

    sched = _scheduler(settings, store, worker)
    asyncio.run(_drive(sched))
    assert "sweep" in calls and "janitor" in calls
    assert ("reap", worker.reap_horizon()) in calls
    # run history recorded with ok status
    for r in store.routines_all():
        if r["workspace_id"] is None:
            runs = store.routine_runs_recent(r["id"])
            assert runs and runs[0]["status"] == "ok"
            assert r["last_status"] == "ok"


def test_sweep_honors_row_toggle_and_legacy_flags(store, settings, worker, monkeypatch):
    routines.ensure_seeds(store, settings)
    calls = []

    async def fake_sweep():
        calls.append("sweep")

    monkeypatch.setattr(worker, "_sweep_once", fake_sweep)
    monkeypatch.setattr(worker, "_janitor_once", lambda: None)

    async def noop_reap(h):
        pass

    monkeypatch.setattr(worker, "_reap_once", noop_reap)
    with store._conn() as c:
        c.execute("UPDATE routines SET last_run_at = NULL")
    sched = _scheduler(settings, store, worker)

    # legacy flag off (sentry unconfigured in the test env) → no fire
    settings.sweep_enabled = True
    assert not settings.sentry_enabled
    asyncio.run(_drive(sched))
    assert calls == []

    # sentry on but the ROW disabled → still no fire (either off means off)
    settings.sentry_org, settings.sentry_auth_token = "acme", "tok"
    row = next(r for r in store.routines_all() if r["kind"] == "sweep")
    store.routine_set(row["id"], enabled=0)
    asyncio.run(_drive(sched))
    assert calls == []

    store.routine_set(row["id"], enabled=1)
    asyncio.run(_drive(sched))
    assert calls == ["sweep"]


def test_reaper_is_non_disableable(store, settings, worker, monkeypatch):
    routines.ensure_seeds(store, settings)
    calls = []

    async def fake_reap(h):
        calls.append("reap")

    monkeypatch.setattr(worker, "_reap_once", fake_reap)
    row = next(r for r in store.routines_all() if r["kind"] == "reaper")
    store.routine_set(row["id"], enabled=0)  # hand-edited DB
    sched = _scheduler(settings, store, worker)
    asyncio.run(_drive(sched))
    assert calls == ["reap"]


def test_builtin_bad_schedule_falls_back_never_dead(store, settings, worker, monkeypatch):
    routines.ensure_seeds(store, settings)
    calls = []

    async def fake_reap(h):
        calls.append("reap")

    monkeypatch.setattr(worker, "_reap_once", fake_reap)
    row = next(r for r in store.routines_all() if r["kind"] == "reaper")
    store.routine_set(row["id"], schedule="garbage")  # hand-edit
    sched = _scheduler(settings, store, worker)
    asyncio.run(_drive(sched))
    assert calls == ["reap"]  # fell back to the derived default, still fired
    assert store.routine_get(row["id"])["enabled"] == 1


def test_workspace_bad_schedule_disables_fail_closed(store, settings, worker,
                                                     monkeypatch):
    with store._conn() as c:
        c.execute("INSERT INTO workspaces (slug, name, created_at, updated_at) "
                  "VALUES ('w1', 'W1', 1, 1)")
    routines.ensure_seeds(store, settings)

    async def never(ctx):  # ensure the kind is registered so scheduling runs
        return "ok", "", 0

    monkeypatch.setitem(routines.REGISTRY, "risk_scan", never)
    ws_id = store.workspace_list()[0]["id"]
    row = next(r for r in store.routines_all()
               if r["workspace_id"] == ws_id and r["kind"] == "risk_scan")
    store.routine_set(row["id"], schedule="whenever")
    sched = _scheduler(settings, store, worker)
    asyncio.run(_drive(sched))
    after = store.routine_get(row["id"])
    assert after["enabled"] == 0
    assert after["last_status"] == "error: bad schedule"


def test_routines_enabled_false_scopes_to_workspace_rows_only(
        store, settings, worker, monkeypatch):
    """The master flag silences Epic I routines ONLY — builtins keep firing
    ('off' must never mean 'less safe')."""
    with store._conn() as c:
        c.execute("INSERT INTO workspaces (slug, name, created_at, updated_at) "
                  "VALUES ('w1', 'W1', 1, 1)")
    routines.ensure_seeds(store, settings)
    settings.routines_enabled = False
    reaps, risks = [], []

    async def fake_reap(h):
        reaps.append(1)

    async def fake_risk(ctx):
        risks.append(1)
        return "ok", "", 0

    monkeypatch.setattr(worker, "_reap_once", fake_reap)
    monkeypatch.setitem(routines.REGISTRY, "risk_scan", fake_risk)
    with store._conn() as c:
        c.execute("UPDATE routines SET last_run_at = NULL WHERE kind IN "
                  "('reaper', 'risk_scan')")
    sched = _scheduler(settings, store, worker)
    asyncio.run(_drive(sched))
    assert reaps == [1] and risks == []

    settings.routines_enabled = True
    with store._conn() as c:
        c.execute("UPDATE routines SET last_run_at = NULL WHERE kind = 'risk_scan'")
    asyncio.run(_drive(sched))
    assert risks == [1]


def test_handler_exception_records_error_and_retries_later(
        store, settings, worker, monkeypatch):
    routines.ensure_seeds(store, settings)

    async def boom(h):
        raise RuntimeError("kaput")

    monkeypatch.setattr(worker, "_reap_once", boom)
    row = next(r for r in store.routines_all() if r["kind"] == "reaper")
    sched = _scheduler(settings, store, worker)
    asyncio.run(_drive(sched))
    after = store.routine_get(row["id"])
    assert after["last_status"] == "error" and "kaput" in after["last_result"]
    runs = store.routine_runs_recent(row["id"])
    assert runs[0]["status"] == "error" and runs[0]["ended_at"] is not None
    # the claim moved last_run_at forward — next due is a full period later,
    # not an immediate hot retry
    assert after["last_run_at"] is not None


def test_fresh_seeded_daily_routine_fires_at_next_occurrence(
        store, settings, worker, monkeypatch):
    """End-to-end regression: a freshly seeded daily row must actually come
    due once its next occurrence passes — with the pre-anchor behavior the
    due time re-slid past `now` at every tick and the standup/memory-upkeep/
    weekly-planning seeds silently never ran."""
    with store._conn() as c:
        c.execute("INSERT INTO workspaces (slug, name, created_at, updated_at) "
                  "VALUES ('w1', 'W1', 1, 1)")
    routines.ensure_seeds(store, settings)
    ws_id = store.workspace_list()[0]["id"]
    row = next(r for r in store.routines_all()
               if r["workspace_id"] == ws_id and r["kind"] == "standup_digest")
    calls = []

    async def fake_standup(ctx):
        calls.append(ctx.routine["id"])
        return "ok", "", 0

    async def quiet(ctx):
        return "quiet", "", 0

    monkeypatch.setitem(routines.REGISTRY, "standup_digest", fake_standup)
    for kind in ("memory_upkeep", "risk_scan", "proposal_scan",
                 "weekly_planning"):
        monkeypatch.setitem(routines.REGISTRY, kind, quiet)

    due = next_due(parse_schedule(row["schedule"]), row["last_run_at"],
                   time.time(), UTC)
    sched = _scheduler(settings, store, worker)
    # a tick BEFORE the occurrence: not due yet
    monkeypatch.setattr(routines, "_time",
                        types.SimpleNamespace(time=lambda: due - 60))
    asyncio.run(_drive(sched))
    assert calls == []
    # a tick AFTER the occurrence: fires exactly once
    monkeypatch.setattr(routines, "_time",
                        types.SimpleNamespace(time=lambda: due + 60))
    asyncio.run(_drive(sched))
    assert calls == [row["id"]]


def test_run_open_failure_never_leaks_inflight(store, settings, worker,
                                               monkeypatch):
    """routine_run_open raising (transient 'database is locked', disk full)
    must still discard the id from _inflight — a leaked id skips the routine
    (including the non-disableable reaper) forever until restart."""
    routines.ensure_seeds(store, settings)
    row = next(r for r in store.routines_all() if r["kind"] == "reaper")
    sched = _scheduler(settings, store, worker)
    sched._inflight.add(row["id"])

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "routine_run_open", boom)

    async def noop(ctx):
        return "ok", "", 0

    asyncio.run(sched._run_one(dict(row), noop, time.time()))
    assert row["id"] not in sched._inflight
    assert store.routine_get(row["id"])["last_status"] == "error"


def test_run_now_request_respects_enablement(store, settings, worker, monkeypatch):
    routines.ensure_seeds(store, settings)
    sched = _scheduler(settings, store, worker)
    sweep = next(r for r in store.routines_all() if r["kind"] == "sweep")
    # sweep ineffective (sentry unconfigured) → refused
    assert sched.request_run(sweep["id"]) is False
    reaper = next(r for r in store.routines_all() if r["kind"] == "reaper")
    assert sched.request_run(reaper["id"]) is True
    assert reaper["id"] in sched._run_now
    assert sched.request_run(999999) is False


def test_builtin_derived_schedule_tracks_live_settings(settings):
    settings.sweep_interval_hours = 2
    assert builtin_default_schedule("sweep", settings) == "every:7200"
    assert builtin_default_schedule("reaper", settings) == "every:300"
    assert builtin_default_schedule("janitor", settings) == "every:86400"


# ---------- API surface ----------


def test_routines_api(tmp_path, monkeypatch):
    import base64

    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test")
    from app import config

    config.get_settings.cache_clear()
    import importlib

    from app import main as main_module

    importlib.reload(main_module)
    AUTH = {"Authorization": "Basic " + base64.b64encode(b"gumo:test").decode()}
    with TestClient(main_module.app) as client:
        data = client.get("/api/routines", headers=AUTH).json()
        kinds = {r["kind"] for r in data["routines"]}
        assert set(routines.BUILTIN_KINDS) <= kinds
        assert set(routines.WORKSPACE_KINDS) <= kinds  # default workspace seeded

        reaper = next(r for r in data["routines"] if r["kind"] == "reaper")
        # reaper: non-disableable, schedule locked
        assert client.put(f"/api/routines/{reaper['id']}", headers=AUTH,
                          json={"enabled": False}).status_code == 400
        assert client.put(f"/api/routines/{reaper['id']}", headers=AUTH,
                          json={"schedule": "every:600"}).status_code == 400

        standup = next(r for r in data["routines"] if r["kind"] == "standup_digest")
        # bad schedule → 400, nothing changes
        r = client.put(f"/api/routines/{standup['id']}", headers=AUTH,
                       json={"schedule": "whenever"})
        assert r.status_code == 400
        # valid edit lands + audited
        r = client.put(f"/api/routines/{standup['id']}", headers=AUTH,
                       json={"schedule": "daily@08:00", "enabled": False})
        assert r.status_code == 200 and r.json()["schedule"] == "daily@08:00"
        store = client.app.state.store
        events = [e for e in store.admin_events_recent(10)
                  if e["kind"] == "routine_config"]
        assert events and "standup_digest" in events[0]["detail"]
        # '' schedule refused on workspace rows (builtin-only revert)
        assert client.put(f"/api/routines/{standup['id']}", headers=AUTH,
                          json={"schedule": ""}).status_code == 400

        # run-now goes through the scheduler (never inline)
        assert client.post(f"/api/routines/{reaper['id']}/run",
                           headers=AUTH).status_code == 200
        # the arm was routed to the scheduler (never inline). It may already
        # have been consumed by a scheduler tick — race-tolerant across the
        # _run_now → _inflight → drained transition; the routine_run_now audit
        # event asserted just below is the durable proof of registration.
        sched = client.app.state.scheduler
        assert (reaper["id"] in sched._run_now
                or reaper["id"] in sched._inflight
                or any(e["kind"] == "routine_run_now"
                       for e in store.admin_events_recent(10)))
        # …and the accepted arm is audited with the admin as actor (a forced
        # firing is otherwise indistinguishable from a scheduled one)
        events = [e for e in store.admin_events_recent(10)
                  if e["kind"] == "routine_run_now"]
        assert events and events[0]["target"] == str(reaper["id"])
        assert events[0]["actor"] == "dashboard:gumo"
        # a member sees no instance rows and no foreign-workspace rows
        client.post("/api/users", headers=AUTH,
                    json={"username": "m1", "password": "password1"})
        member = {"Authorization": "Basic "
                  + base64.b64encode(b"m1:password1").decode()}
        assert client.get("/api/routines", headers=member).json()["routines"] == []
    config.get_settings.cache_clear()


def test_routine_runs_prune_keeps_latest(store, settings):
    routines.ensure_seeds(store, settings)
    rid = store.routines_all()[0]["id"]
    for _ in range(30):
        run = store.routine_run_open(rid, "reaper", None)
        store.routine_run_close(run, "ok")
    with store._conn() as c:
        c.execute("UPDATE routine_runs SET started_at = ?", (time.time() - 400 * 86400,))
    deleted = store.routine_runs_prune(90, keep_latest=20)
    assert deleted == 10
    assert len(store.routine_runs_recent(rid, 100)) == 20

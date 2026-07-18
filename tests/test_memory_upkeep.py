"""Epic I3: the memory upkeep routine — threshold-gated, bounded one per
repo per cooldown, budget/cap-aware with VISIBLE skips, one-time no-cache
note."""

import asyncio
import json
import time
from pathlib import Path

import pytest

from app import routines
from app.routines import RoutineContext


@pytest.fixture()
def ws(store, settings):
    with store._conn() as c:
        c.execute("INSERT INTO workspaces (slug, name, created_at, updated_at) "
                  "VALUES ('w1', 'W1', 1, 1)")
    ws = store.workspace_list()[0]
    with store._conn() as c:
        for slug, repo in (("demo", "acme/demo"), ("web", "acme/web")):
            c.execute("INSERT INTO workspace_repos (workspace_id, slug, repo, base) "
                      "VALUES (?, ?, ?, 'main')", (ws["id"], slug, repo))
    routines.ensure_seeds(store, settings)
    return ws


def _write_meta(settings, project, staleness, fetched_at=None):
    cache = Path(settings.data_dir) / "memory" / project
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "architecture.md").write_text("# arch")
    (cache / "meta.json").write_text(json.dumps({
        "commit_sha": "abc", "fetched_at": fetched_at or time.time(),
        "files": {}, "staleness_commits": staleness,
    }))


def _ctx(settings, store, worker, ws):
    routine = next(r for r in store.routines_all()
                   if r["workspace_id"] == ws["id"] and r["kind"] == "memory_upkeep")
    return RoutineContext(settings=settings, store=store, worker=worker,
                          workspaces=None, routine=routine, now=time.time())


def _run(ctx):
    return asyncio.run(routines._handle_memory_upkeep(ctx))


def test_threshold_zero_is_inert(store, settings, worker, ws):
    _write_meta(settings, "demo", staleness=500)
    assert settings.memory_staleness_threshold == 0
    status, detail, items = _run(_ctx(settings, store, worker, ws))
    assert status == "skipped" and "inert" in detail and items == 0
    assert store.get("mem-demo") is None


def test_stale_cache_queues_once_with_note(store, settings, worker, ws):
    settings.memory_staleness_threshold = 10
    _write_meta(settings, "demo", staleness=25,
                fetched_at=time.time() - 3 * 86400)
    _write_meta(settings, "web", staleness=2)  # fresh — untouched
    status, detail, items = _run(_ctx(settings, store, worker, ws))
    assert status == "ok" and "queued 1" in detail
    mem = store.get("mem-demo")
    assert mem is not None and mem["kind"] == "memory"
    assert mem["source"] == "routine"  # provenance visible
    assert store.get("mem-web") is None
    notes = [n for n in store.inbox_items_open(None) if n["kind"] == "routine_note"]
    assert len(notes) == 1 and "25 commits stale" in notes[0]["title"]
    assert "3d ago" in notes[0]["body"]  # fetched_at age surfaced


def test_cooldown_blocks_second_fire_including_human_bootstrap(
        store, settings, worker, ws):
    settings.memory_staleness_threshold = 10
    _write_meta(settings, "demo", staleness=25)
    # a HUMAN bootstrap ran yesterday and finished — counts toward the bound
    worker.intake_memory("demo")
    store.set_status("mem-demo", "pr_opened")
    status, detail, _ = _run(_ctx(settings, store, worker, ws))
    assert status == "skipped" and "bound" in detail
    # an ACTIVE bootstrap also blocks
    store.set_status("mem-demo", "running")
    status, detail, _ = _run(_ctx(settings, store, worker, ws))
    assert "bound" in detail
    # bound expired → fires again
    with store._conn() as c:
        c.execute("UPDATE jobs SET updated_at = ?, status = 'pr_opened' "
                  "WHERE issue_id = 'mem-demo'", (time.time() - 8 * 86400,))
    status, detail, _ = _run(_ctx(settings, store, worker, ws))
    assert status == "ok" and "queued 1" in detail


def test_daily_cap_skip_is_visible(store, settings, worker, ws):
    settings.memory_staleness_threshold = 10
    settings.max_runs_per_day = 0
    _write_meta(settings, "demo", staleness=25)
    status, detail, _ = _run(_ctx(settings, store, worker, ws))
    assert status == "skipped" and "daily run cap" in detail
    assert store.get("mem-demo") is None


def test_budget_skip_is_visible(store, settings, worker, ws):
    settings.memory_staleness_threshold = 10
    settings.budget_monthly_usd = 5
    _write_meta(settings, "demo", staleness=25)
    store.feature_intake("feat-b", title="t", project="demo")
    store.set_fields("feat-b", workspace_id=ws["id"])
    rid = store.stage_run_open("feat-b", 0, 1)
    store.stage_run_close(rid, "done", cost_usd=9.0)
    status, detail, _ = _run(_ctx(settings, store, worker, ws))
    assert status == "skipped" and "budget" in detail
    assert store.get("mem-demo") is None


def test_no_cache_emits_one_time_note(store, settings, worker, ws):
    settings.memory_staleness_threshold = 10
    status, detail, items = _run(_ctx(settings, store, worker, ws))
    notes = [n for n in store.inbox_items_open(None) if n["kind"] == "routine_note"]
    assert {json.loads(n["refs"])["project"] for n in notes} == {"demo", "web"}
    assert items == 2
    # second pass: dedupe — no new notes
    status, detail, items = _run(_ctx(settings, store, worker, ws))
    assert items == 0
    assert len([n for n in store.inbox_items_open(None)
                if n["kind"] == "routine_note"]) == 2


def test_cached_exists_false_shape_is_guarded(store, settings, worker, ws,
                                              monkeypatch):
    """Amendment 11: MemoryReader.cached() may return {'exists': False} with
    NO 'meta' key — the handler must not KeyError."""
    settings.memory_staleness_threshold = 10
    from app import memory as memory_mod

    monkeypatch.setattr(memory_mod.MemoryReader, "cached",
                        lambda self, project: {"exists": False})
    status, detail, items = _run(_ctx(settings, store, worker, ws))
    assert status in ("ok", "skipped")

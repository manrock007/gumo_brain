"""Epic I6: the weekly planning pack — receipts math (open runs excluded,
median gate wait), deterministic ranking, weekly dedupe, open-proposals-only."""

import asyncio
import time

import pytest

from app import digests, routines
from app.routines import RoutineContext


@pytest.fixture()
def ws(store, settings):
    with store._conn() as c:
        c.execute("INSERT INTO workspaces (slug, name, created_at, updated_at) "
                  "VALUES ('w1', 'W1', 1, 1)")
    routines.ensure_seeds(store, settings)
    return store.workspace_list()[0]


def _seed_run(store, job_id, started_ago, cost=1.0, status="done",
              gate_wait_s=None):
    rid = store.stage_run_open(job_id, 5, 1)
    started = time.time() - started_ago
    with store._conn() as c:
        c.execute("UPDATE stage_runs SET started_at = ?, ended_at = ?, "
                  "result_status = ?, cost_usd = ? WHERE id = ?",
                  (started, started + 60, status, cost, rid))
        if gate_wait_s is not None:
            c.execute("UPDATE stage_runs SET gate_posted_at = ?, "
                      "gate_answered_at = ? WHERE id = ?",
                      (started + 100, started + 100 + gate_wait_s, rid))
    return rid


def test_receipts_math_vs_prior_week(store, settings, ws):
    now = time.time()
    store.feature_intake("feat-p1", title="t", project="demo")
    store.set_fields("feat-p1", workspace_id=ws["id"])
    day = 86400
    # THIS week: two closed runs (waits 2h and 6h → median 6h with the upper-
    # middle rule on an even list), one OPEN run (excluded from denominators)
    _seed_run(store, "feat-p1", 2 * day, cost=3.0, gate_wait_s=2 * 3600)
    _seed_run(store, "feat-p1", 3 * day, cost=2.0, gate_wait_s=6 * 3600)
    _seed_run(store, "feat-p1", 1 * day, cost=0.0, status="")  # open
    # one redo this week
    store.guidance_add("feat-p1", 5, "redo", "again", "dashboard:x")
    # LAST week: one run, no redos
    _seed_run(store, "feat-p1", 9 * day, cost=10.0, gate_wait_s=1 * 3600)

    pack = digests.build_planning_pack(store, settings,
                                       store.workspace_get(ws["id"]))
    tw, lw = pack["receipts"]["this_week"], pack["receipts"]["last_week"]
    assert tw["runs"] == 2          # the open run is excluded
    assert tw["cost_usd"] == pytest.approx(5.0)
    assert tw["median_gate_wait_s"] == pytest.approx(6 * 3600)
    assert tw["answered_gates"] == 2
    assert tw["redo_rate"] == pytest.approx(0.5)
    assert lw["runs"] == 1 and lw["cost_usd"] == pytest.approx(10.0)
    assert "Receipts" in pack["body"] and "Redo rate: 50%" in pack["body"]


def test_ranking_deterministic(store, settings, ws):
    store.inbox_item_add("proposal", "r1", "friction small",
                         refs={"source_kind": "friction", "count": 3},
                         workspace_id=ws["id"])
    store.inbox_item_add("proposal", "r2", "regressed outcome",
                         refs={"source_kind": "outcome", "verdict": "regressed"},
                         workspace_id=ws["id"])
    store.inbox_item_add("proposal", "r3", "sentry cluster",
                         refs={"source_kind": "sentry-cluster", "count": 5},
                         workspace_id=ws["id"])
    store.inbox_item_add("proposal", "r4", "friction big",
                         refs={"source_kind": "friction", "count": 9},
                         workspace_id=ws["id"])
    pack = digests.build_planning_pack(store, settings,
                                       store.workspace_get(ws["id"]))
    titles = {i["id"]: i["title"] for i in store.inbox_items_open(None)}
    order = [titles[i] for i in pack["ranked"]]
    assert order == ["regressed outcome", "sentry cluster", "friction big",
                     "friction small"]
    assert digests.RANKING_FORMULA in pack["body"]  # transparency requirement


def test_pack_lists_open_proposals_only(store, settings, ws):
    store.inbox_item_add("proposal", "o1", "open one", workspace_id=ws["id"])
    store.inbox_item_add("proposal", "o2", "dismissed one", workspace_id=ws["id"])
    dismissed = store.inbox_item_by_key("proposal", "o2")
    store.inbox_item_resolve(dismissed["id"], "dismissed", "dashboard:x")
    store.inbox_item_add("risk_alert", "o3", "an alert", workspace_id=ws["id"])
    pack = digests.build_planning_pack(store, settings,
                                       store.workspace_get(ws["id"]))
    assert "open one" in pack["body"]
    assert "dismissed one" not in pack["body"]
    assert "an alert" not in pack["body"]


def test_weekly_dedupe_and_predecessor_expiry(store, settings, ws):
    sent = []

    class FakeWS:
        async def notify_text(self, w, text):
            sent.append(text)

    routine = next(r for r in store.routines_all()
                   if r["workspace_id"] == ws["id"] and r["kind"] == "weekly_planning")
    ctx = RoutineContext(settings=settings, store=store, worker=None,
                         workspaces=FakeWS(), routine=routine, now=time.time())
    status, _, items = asyncio.run(routines._handle_weekly_planning(ctx))
    assert status == "ok" and items == 1
    assert len(sent) == 1 and "planning pack" in sent[0]
    # re-fire in the same ISO week: deduped, no second send
    status, _, items = asyncio.run(routines._handle_weekly_planning(ctx))
    assert items == 0 and len(sent) == 1
    packs = [i for i in store.inbox_items_open(None) if i["kind"] == "planning_pack"]
    assert len(packs) == 1
    # last week's still-open pack expires when the new one lands
    with store._conn() as c:
        c.execute("UPDATE inbox_items SET dedupe_key = ? WHERE id = ?",
                  (f"{ws['id']}:2000-W01", packs[0]["id"]))
    status, _, items = asyncio.run(routines._handle_weekly_planning(ctx))
    assert items == 1
    old = store.inbox_item_by_key("planning_pack", f"{ws['id']}:2000-W01")
    assert old["status"] == "expired" and old["status_by"] == "engine"

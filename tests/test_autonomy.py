"""Epic C — graduated autonomy: scorer, clawback, gate resolution, loop."""

import asyncio
import json
import time

import pytest

from app import autonomy


def _ws(store, slug="w1", repos=("web",)):
    ws = store.workspace_create(slug, slug.upper())
    store.workspace_repos_replace(
        ws["id"], [{"slug": s, "repo": f"acme/{s}"} for s in repos])
    return ws["id"]


def _feature(store, job_id, ws_id, project="web"):
    store.feature_intake(job_id, title="F", project=project, stage=0)
    store.set_fields(job_id, workspace_id=ws_id)
    return job_id


def _run(store, job_id, stage, status="done", answered=True, wait=600.0,
         action="proceed", started=None):
    """Seed one closed stage_runs row with explicit timestamps."""
    rid = store.stage_run_open(job_id, stage, 1)
    started = started if started is not None else time.time() - 3600
    fields = {"started_at": started, "ended_at": started + 300,
              "result_status": status}
    if answered:
        fields.update(gate_posted_at=started + 300,
                      gate_answered_at=started + 300 + wait,
                      gate_action=action)
    with store._conn() as c:
        cols = ", ".join(f"{k} = ?" for k in fields)
        c.execute(f"UPDATE stage_runs SET {cols} WHERE id = ?",
                  (*fields.values(), rid))
    return rid


class TestScorer:
    def test_clean_history_earns_level_3(self, store, settings):
        ws = _ws(store)
        _feature(store, "feat-s1", ws)
        for _ in range(6):
            _run(store, "feat-s1", 2)
        res = autonomy.compute(store, settings)
        assert res["cells"] == 1 and res["changed"] == 1
        row = store.autonomy_score_get(ws, "web", 2)
        assert row["level"] == 3
        assert row["score"] > 0.9
        assert row["sample_runs"] == 6
        inputs = json.loads(row["inputs"])
        assert inputs["sample"] == 6
        assert inputs["clean_rate"] == 1.0
        assert inputs["redo_rate"] == 0.0
        assert inputs["clean_streak"] == 6
        assert inputs["answered_gates"] == 6
        events = store.autonomy_events_recent([ws])
        assert [e["kind"] for e in events] == ["level_change"]
        assert "level 0 → 3" in events[0]["detail"]
        assert events[0]["actor"] == "engine"

    def test_min_sample_stays_level_0(self, store, settings):
        ws = _ws(store)
        _feature(store, "feat-s2", ws)
        for _ in range(3):  # below autonomy_min_runs (5)
            _run(store, "feat-s2", 2)
        autonomy.compute(store, settings)
        row = store.autonomy_score_get(ws, "web", 2)
        assert row["level"] == 0
        assert row["sample_runs"] == 3

    def test_window_excludes_old_runs(self, store, settings):
        ws = _ws(store)
        _feature(store, "feat-s3", ws)
        old = time.time() - (settings.autonomy_window_days + 10) * 86400
        for _ in range(6):
            _run(store, "feat-s3", 2, started=old)
        for _ in range(2):
            _run(store, "feat-s3", 2)
        autonomy.compute(store, settings)
        row = store.autonomy_score_get(ws, "web", 2)
        assert row["sample_runs"] == 2  # old runs never counted
        assert row["level"] == 0

    def test_open_and_skipped_runs_excluded_from_denominators(self, store, settings):
        ws = _ws(store)
        _feature(store, "feat-s4", ws)
        for _ in range(5):
            _run(store, "feat-s4", 2)
        store.stage_run_open("feat-s4", 2, 1)  # live run: result_status=''
        _run(store, "feat-s4", 2, status="interrupted", answered=False)
        _run(store, "feat-s4", 2, status="skipped_single_group", answered=False)
        autonomy.compute(store, settings)
        row = store.autonomy_score_get(ws, "web", 2)
        assert row["sample_runs"] == 5
        assert json.loads(row["inputs"])["clean_rate"] == 1.0

    def test_broken_streak_demotes_level_3_to_2(self, store, settings):
        ws = _ws(store)
        _feature(store, "feat-s5", ws)
        for _ in range(11):
            _run(store, "feat-s5", 2)
        # most recent run failed: score stays >= 0.90 but the streak is 0
        _run(store, "feat-s5", 2, status="stage_fail", answered=True,
             action="redo", started=time.time() - 60)
        autonomy.compute(store, settings)
        row = store.autonomy_score_get(ws, "web", 2)
        assert row["score"] >= 0.90
        assert row["level"] == 2
        assert json.loads(row["inputs"])["clean_streak"] == 0

    def test_level_3_requires_a_human_answered_gate(self, store, settings):
        """Self-reinforcement bound: a cell whose window contains only
        auto-advanced runs (no gate_answered_at anywhere) gets full credit on
        the redo/latency terms — it must cap at level 2, never 3."""
        ws = _ws(store)
        _feature(store, "feat-s6", ws)
        for _ in range(8):
            _run(store, "feat-s6", 2, answered=False)  # clean but unreviewed
        autonomy.compute(store, settings)
        row = store.autonomy_score_get(ws, "web", 2)
        assert row["score"] >= 0.90
        assert row["level"] == 2
        assert json.loads(row["inputs"])["answered_gates"] == 0

    def test_redo_attributed_to_target_stage_not_parked_stage(self, store, settings):
        """A '/redo P2 …' answered at the P4 gate penalizes P2 (guidance_log
        target), never the innocent parked stage P4."""
        ws = _ws(store)
        _feature(store, "feat-s7", ws)
        for _ in range(6):
            _run(store, "feat-s7", 2)
            _run(store, "feat-s7", 4)
        # the retargeted redo: guidance lands on stage 2, gate_action on stage 4
        store.guidance_add("feat-s7", 2, "redo", "wrong recon", "dashboard:m")
        store.stage_run_gate_answered("feat-s7", 4, "redo")
        autonomy.compute(store, settings)
        p2 = json.loads(store.autonomy_score_get(ws, "web", 2)["inputs"])
        p4 = json.loads(store.autonomy_score_get(ws, "web", 4)["inputs"])
        assert p2["redo_count"] == 1 and p2["redo_rate"] > 0
        assert p4["redo_count"] == 0 and p4["redo_rate"] == 0

    def test_slow_gate_answers_drag_the_score(self, store, settings):
        ws = _ws(store)
        _feature(store, "feat-s8", ws)
        # answered after 4x the 24h SLA -> latency_factor 0
        for _ in range(6):
            _run(store, "feat-s8", 2, wait=4 * 24 * 3600)
        autonomy.compute(store, settings)
        row = store.autonomy_score_get(ws, "web", 2)
        assert json.loads(row["inputs"])["latency_factor"] == 0.0
        assert row["score"] < 0.90

    def test_shepherd_rounds_affect_code_stages_only(self, store, settings):
        ws = _ws(store)
        _feature(store, "feat-s9", ws)
        for _ in range(6):
            _run(store, "feat-s9", 2)
            _run(store, "feat-s9", 5)
        store.pr_add("feat-s9", "https://github.com/acme/web/pull/9")
        store.pr_set("https://github.com/acme/web/pull/9", review_rounds=6)  # at the cap
        autonomy.compute(store, settings)
        doc = json.loads(store.autonomy_score_get(ws, "web", 2)["inputs"])
        code = json.loads(store.autonomy_score_get(ws, "web", 5)["inputs"])
        assert doc["rounds_factor"] == 1.0        # doc stages: neutral
        assert code["rounds_factor"] == 0.0       # 6/6 rounds burns the term
        assert code["avg_review_rounds"] == 6.0

    def test_recompute_is_idempotent(self, store, settings):
        ws = _ws(store)
        _feature(store, "feat-s10", ws)
        for _ in range(6):
            _run(store, "feat-s10", 2)
        assert autonomy.compute(store, settings)["changed"] == 1
        assert autonomy.compute(store, settings)["changed"] == 0
        assert store.autonomy_score_get(ws, "web", 2)["level"] == 3

    def test_emptied_window_decays_a_stored_level(self, store, settings):
        """A cell whose runs age out of the window is revisited on the next
        pass and decays to level 0 — a stale level 3 never lives forever."""
        ws = _ws(store)
        _feature(store, "feat-s11", ws)
        rids = [_run(store, "feat-s11", 2) for _ in range(6)]
        autonomy.compute(store, settings)
        assert store.autonomy_score_get(ws, "web", 2)["level"] == 3
        old = time.time() - (settings.autonomy_window_days + 5) * 86400
        with store._conn() as c:
            for rid in rids:
                c.execute("UPDATE stage_runs SET started_at = ? WHERE id = ?", (old, rid))
        res = autonomy.compute(store, settings)
        assert res["changed"] == 1
        row = store.autonomy_score_get(ws, "web", 2)
        assert row["level"] == 0 and row["sample_runs"] == 0


class TestClawback:
    def _earn_level_3(self, store, settings, ws, job_id="feat-c1"):
        _feature(store, job_id, ws)
        for _ in range(6):
            _run(store, job_id, 2)
        autonomy.compute(store, settings)
        assert store.autonomy_score_get(ws, "web", 2)["level"] == 3

    def test_clawback_zeroes_and_stamps(self, store, settings):
        ws = _ws(store)
        self._earn_level_3(store, settings, ws)
        n = autonomy.clawback(store, settings, ws, 2, "web", actor="dashboard:boss")
        assert n == 1
        row = store.autonomy_score_get(ws, "web", 2)
        assert row["level"] == 0 and row["score"] == 0
        assert row["clawback_at"] is not None
        ev = store.autonomy_events_recent([ws])
        assert ev[0]["kind"] == "clawback" and ev[0]["actor"] == "dashboard:boss"

    def test_pre_clawback_runs_never_count_again(self, store, settings):
        ws = _ws(store)
        self._earn_level_3(store, settings, ws)
        autonomy.clawback(store, settings, ws, 2, "web", actor="dashboard:boss")
        autonomy.compute(store, settings)  # same runs, all pre-clawback
        row = store.autonomy_score_get(ws, "web", 2)
        assert row["level"] == 0 and row["sample_runs"] == 0

    def test_post_clawback_runs_re_earn(self, store, settings):
        ws = _ws(store)
        self._earn_level_3(store, settings, ws)
        autonomy.clawback(store, settings, ws, 2, "web", actor="dashboard:boss")
        for _ in range(6):  # fresh, post-clawback track record
            _run(store, "feat-c1", 2, started=time.time() + 1)
        autonomy.compute(store, settings, now=time.time() + 10)
        row = store.autonomy_score_get(ws, "web", 2)
        assert row["level"] == 3 and row["sample_runs"] == 6
        assert row["clawback_at"] is not None  # the stamp is never cleared

    def test_conditional_upsert_never_resurrects_a_clawed_cell(self, store, settings):
        """A clawback landing mid-compute (after the pass's start timestamp)
        wins: the upsert is filtered out and no level_change event fires."""
        ws = _ws(store)
        self._earn_level_3(store, settings, ws)
        compute_started = time.time() - 30  # a pass that started before...
        autonomy.clawback(store, settings, ws, 2, "web", actor="dashboard:boss")
        res = store.autonomy_score_upsert(ws, "web", 2, level=3, score=0.99,
                                          inputs_json="{}", sample_runs=6,
                                          computed_started=compute_started)
        assert res["applied"] is False
        assert res["level"] == 0  # the stored (clawed) level, post re-read
        row = store.autonomy_score_get(ws, "web", 2)
        assert row["level"] == 0 and row["clawback_at"] is not None

    def test_workspace_wide_clawback_covers_stale_slugs(self, store, settings):
        """project=None claws back every slug that ever held a cell — including
        one since removed from the workspace's repo set — plus current repos."""
        ws = _ws(store, repos=("web", "api"))
        store.autonomy_score_upsert(ws, "gone", 5, level=3, score=0.95,
                                    inputs_json="{}", sample_runs=9,
                                    computed_started=time.time())
        n = autonomy.clawback(store, settings, ws, 5, None, actor="dashboard:boss")
        assert n == 3  # web + api (current) + gone (historical cell)
        assert store.autonomy_score_get(ws, "gone", 5)["level"] == 0
        for slug in ("web", "api", "gone"):
            row = store.autonomy_score_get(ws, slug, 5)
            assert row["level"] == 0 and row["clawback_at"] is not None
        kinds = [e["kind"] for e in store.autonomy_events_recent([ws])]
        assert kinds.count("clawback") == 3


class TestResolveGate:
    def _job(self, ws_id, gate_mode="full", project="web"):
        return {"issue_id": "feat-r1", "gate_mode": gate_mode,
                "workspace_id": ws_id, "project": project}

    def test_always_gate_pin_beats_level_3(self, store, settings):
        ws = _ws(store)
        settings.autonomy_auto_level = 3
        store.autonomy_score_upsert(ws, "web", 5, 3, 0.95, "{}", 9, time.time())
        store.autonomy_pin_set(ws, 5, "always_gate", "dashboard:boss")
        mode, reason = autonomy.resolve_gate(store, settings, self._job(ws), 5)
        assert mode == "gate" and reason == "pin: always_gate"

    def test_always_gate_pin_beats_light_mode(self, store, settings):
        ws = _ws(store)
        store.autonomy_pin_set(ws, 7, "always_gate", "dashboard:boss")
        mode, _ = autonomy.resolve_gate(store, settings, self._job(ws, "light"), 7)
        assert mode == "gate"

    def test_always_auto_pin_advances_full_mode_stages_0_to_8(self, store, settings):
        ws = _ws(store)
        for stage in range(9):
            store.autonomy_pin_set(ws, stage, "always_auto", "dashboard:boss")
            mode, reason = autonomy.resolve_gate(store, settings, self._job(ws), stage)
            assert (mode, reason) == ("auto", "pin: always_auto"), stage

    def test_stage_9_never_auto_even_pinned(self, store, settings):
        ws = _ws(store)
        store.autonomy_pin_set(ws, 9, "always_auto", "dashboard:boss")  # defensive
        settings.autonomy_auto_level = 1
        mode, reason = autonomy.resolve_gate(store, settings, self._job(ws), 9)
        assert (mode, reason) == ("gate", "terminal stage")

    def test_computed_level_needs_the_opt_in(self, store, settings):
        ws = _ws(store)
        store.autonomy_score_upsert(ws, "web", 5, 3, 0.95,
                                    json.dumps({"clean_streak": 14}), 14, time.time())
        job = self._job(ws)
        # default autonomy_auto_level=0: computed levels never auto-advance
        assert settings.autonomy_auto_level == 0
        assert autonomy.resolve_gate(store, settings, job, 5)[0] == "gate"
        settings.autonomy_auto_level = 3
        mode, reason = autonomy.resolve_gate(store, settings, job, 5)
        assert mode == "auto"
        assert reason == "autonomy level 3, 14 clean runs"

    def test_level_below_auto_level_gates(self, store, settings):
        ws = _ws(store)
        settings.autonomy_auto_level = 3
        store.autonomy_score_upsert(ws, "web", 5, 2, 0.8, "{}", 9, time.time())
        assert autonomy.resolve_gate(store, settings, self._job(ws), 5)[0] == "gate"

    def test_out_of_range_auto_level_disables_never_clamps(self, store, settings):
        ws = _ws(store)
        store.autonomy_score_upsert(ws, "web", 5, 3, 0.95, "{}", 9, time.time())
        for bad in (0, -1, 4, 99):
            settings.autonomy_auto_level = bad
            assert autonomy.resolve_gate(store, settings, self._job(ws), 5)[0] == "gate", bad

    def test_light_mode_still_applies_with_a_workspace(self, store, settings):
        ws = _ws(store)
        job = self._job(ws, gate_mode="light")
        assert autonomy.resolve_gate(store, settings, job, 7) == ("auto", "light gate mode")
        assert autonomy.resolve_gate(store, settings, job, 3)[0] == "gate"

    def test_missing_workspace_skips_pins_and_levels_only(self, store, settings):
        """The legacy light-mode path is workspace-independent — unstamped jobs
        keep it; pins/levels are simply out of reach (fail closed)."""
        settings.autonomy_auto_level = 3
        job = self._job(None, gate_mode="light")
        assert autonomy.resolve_gate(store, settings, job, 7) == ("auto", "light gate mode")
        assert autonomy.resolve_gate(store, settings, job, 3)[0] == "gate"
        assert autonomy.resolve_gate(store, settings, self._job(None), 5)[0] == "gate"

    def test_disabled_autonomy_reduces_to_legacy_light_mode(self, store, settings):
        ws = _ws(store)
        settings.autonomy_enabled = False
        settings.autonomy_auto_level = 3
        store.autonomy_pin_set(ws, 5, "always_auto", "dashboard:boss")
        store.autonomy_score_upsert(ws, "web", 5, 3, 0.95, "{}", 9, time.time())
        assert autonomy.resolve_gate(store, settings, self._job(ws), 5)[0] == "gate"
        light = self._job(ws, gate_mode="light")
        assert autonomy.resolve_gate(store, settings, light, 5)[0] == "auto"
        # an always_gate pin cannot fire either while disabled — pure legacy
        store.autonomy_pin_set(ws, 7, "always_gate", "dashboard:boss")
        assert autonomy.resolve_gate(store, settings, light, 7)[0] == "auto"


class TestWorkerLoop:
    def test_loop_exits_when_disabled(self, worker):
        worker.settings.autonomy_enabled = False
        asyncio.run(worker.autonomy_forever())  # returns immediately or hangs

    def test_compute_through_the_worker_shape(self, worker):
        """One pass exactly as autonomy_forever invokes it (thread offload)."""
        ws = _ws(worker.store)
        _feature(worker.store, "feat-w1", ws)
        for _ in range(6):
            _run(worker.store, "feat-w1", 2)

        async def one_pass():
            return await asyncio.to_thread(
                autonomy.compute, worker.store, worker.settings)

        res = asyncio.run(one_pass())
        assert res == {"cells": 1, "changed": 1}
        assert worker.store.autonomy_score_get(ws, "web", 2)["level"] == 3


class TestDbPrimitives:
    def test_pin_set_clear_list_round_trip(self, store):
        ws = _ws(store)
        store.autonomy_pin_set(ws, 7, "always_gate", "dashboard:boss")
        store.autonomy_pin_set(ws, 5, "always_auto", "dashboard:boss")
        pins = store.autonomy_pins_for(ws)
        assert pins[7]["pin"] == "always_gate" and pins[5]["pin"] == "always_auto"
        assert pins[7]["set_by"] == "dashboard:boss"
        store.autonomy_pin_set(ws, 7, "always_auto", "dashboard:other")  # replace
        assert store.autonomy_pins_for(ws)[7]["pin"] == "always_auto"
        store.autonomy_pin_clear(ws, 7)
        assert 7 not in store.autonomy_pins_for(ws)
        assert [p["stage"] for p in store.autonomy_pins_all()] == [5]

    def test_score_upsert_preserves_clawback_at(self, store):
        ws = _ws(store)
        store.autonomy_clawback(ws, 2, "web")
        stamp = store.autonomy_score_get(ws, "web", 2)["clawback_at"]
        assert stamp is not None
        res = store.autonomy_score_upsert(ws, "web", 2, 1, 0.6, "{}", 6,
                                          computed_started=time.time())
        assert res["applied"] is True  # compute started after the clawback
        row = store.autonomy_score_get(ws, "web", 2)
        assert row["level"] == 1
        assert row["clawback_at"] == stamp  # the stamp survives every upsert

    def test_events_are_insert_only_and_ordered(self, store):
        ws = _ws(store)
        store.autonomy_event_add("pin_set", workspace_id=ws, stage=7,
                                 detail="d1", actor="dashboard:a")
        store.autonomy_event_add("clawback", workspace_id=ws, stage=7,
                                 detail="d2", actor="dashboard:b")
        store.autonomy_event_add("auto_advance", workspace_id=None, job_id="feat-x")
        ev = store.autonomy_events_recent([ws])
        assert [e["kind"] for e in ev] == ["clawback", "pin_set"]  # newest first
        allv = store.autonomy_events_recent(None)
        assert [e["kind"] for e in allv] == ["auto_advance", "clawback", "pin_set"]
        assert store.autonomy_events_recent([]) == []

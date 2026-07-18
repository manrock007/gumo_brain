"""Epic A1: ClickUp answer attribution. Gate verbs carry their commenter; an
unmapped commenter is refused (strictness: workspace/instance
require_attributed_answers — 'auto' = strict once any user is mapped) with
exactly ONE explanatory reply per comment, and the refusal can never wedge a
comment stream (a dedupe-hit is skipped, so the real owner's later verb on
the same stream still lands)."""

import asyncio
import time

import pytest


class FakeCU:
    enabled = True

    def __init__(self, streams=None):
        self.streams = streams or {}  # task_id -> [comment dicts]
        self.posted = []              # (task_id, text)
        self.assigned = []            # (task_id, user_id)

    async def comments(self, task_id):
        return list(self.streams.get(task_id, []))

    async def comment(self, task_id, text):
        self.posted.append((task_id, text))

    async def set_status(self, task_id, state):
        pass

    async def set_assignee(self, task_id, user_id):
        self.assigned.append((task_id, user_id))

    async def field_append(self, task_id, field, line):
        return True

    async def field_set(self, task_id, field, value):
        return True


def _comment(cid, text, user_id=None, username=None, age=-3600):
    """A ClickUp comment shaped like the poller sees it. Default date is in
    the FUTURE relative to the job row (the no-marker date fence must pass).
    user_id/username omitted -> legacy fake shape (no identity keys)."""
    c = {"id": cid, "text": text, "date": time.time() - age}
    if user_id is not None:
        c["user_id"] = user_id
    if username is not None:
        c["username"] = username
    return c


def _park_feature(worker, job_id="feat-at1", stage=3, **fields):
    worker.intake_feature(job_id, title="F", project="web", request="r",
                          clickup_task_id="cu1", clickup_task_url="https://cu/x",
                          **fields)
    worker.store.set_fields(job_id, stage=stage)
    worker.store.set_status(job_id, "awaiting_input")
    return worker.store.get(job_id)


def _map_user(store, username, cu_id, role="member"):
    store.user_create(username, "hash", role=role)
    store.user_set(username, clickup_user_id=cu_id)


class TestMappedCommenter:
    def test_verb_applies_with_attributed_via(self, worker):
        _map_user(worker.store, "jane", "333")
        worker.clickup = FakeCU({"cu1": [_comment("c1", "/proceed looks good",
                                                  user_id="333", username="jane-cu")]})
        job = _park_feature(worker)
        assert asyncio.run(worker._scan_verbs(job, "cu1", use_marker=True)) is True
        row = worker.store.get("feat-at1")
        assert row["status"] == "queued" and row["stage"] == 4
        entry = worker.store.guidance_for("feat-at1")[-1]
        assert entry["via"] == "clickup:jane"  # the CtrlLoop identity, not the CU name


class TestUnmappedRefusal:
    def test_on_mode_refuses_once_marker_path(self, worker):
        worker.settings.require_attributed_answers = "on"
        worker.clickup = FakeCU({"cu1": [
            _comment("c0", "gate posted", user_id="0", username="engine"),
            _comment("c1", "/proceed go", user_id="999", username="rando"),
        ]})
        job = _park_feature(worker)
        worker.store.set_fields("feat-at1", comment_marker="c0")  # true marker path
        job = worker.store.get("feat-at1")
        assert asyncio.run(worker._scan_verbs(job, "cu1", use_marker=True)) is True
        row = worker.store.get("feat-at1")
        assert row["status"] == "awaiting_input"  # NOT applied
        assert row["comment_marker"] == "c1"      # marker advanced (DB before reply)
        replies = [t for _, t in worker.clickup.posted if "NOT applied" in t]
        assert len(replies) == 1 and "rando" in replies[0]
        events = worker.store.gate_events_for("feat-at1")
        assert [e["kind"] for e in events] == ["refused_unattributed"]
        assert events[0]["ref"] == "c1" and "999" in events[0]["actor"]
        # repeated polls: marker skips it — still exactly one reply
        asyncio.run(worker._scan_verbs(worker.store.get("feat-at1"), "cu1", use_marker=True))
        assert len([t for _, t in worker.clickup.posted if "NOT applied" in t]) == 1

    def test_subtask_stream_refuses_once_across_polls(self, worker):
        """No-marker path: the date fence re-encounters the refused comment on
        EVERY poll — the gate_events dedupe is what keeps it to one reply."""
        worker.settings.require_attributed_answers = "on"
        worker.clickup = FakeCU({"sub1": [_comment("s1", "/proceed go",
                                                   user_id="999", username="rando")]})
        job = _park_feature(worker, "feat-at2")
        for _ in range(3):
            asyncio.run(worker._scan_verbs(worker.store.get("feat-at2"), "sub1",
                                           use_marker=False, after=0))
        assert len([t for _, t in worker.clickup.posted if "NOT applied" in t]) == 1
        assert worker.store.get("feat-at2")["status"] == "awaiting_input"

    def test_refused_comment_never_wedges_the_stream(self, worker):
        """Blocker 3: after a refusal, the ACTUAL owner's later /proceed on the
        SAME subtask stream must still be scanned and applied."""
        worker.settings.require_attributed_answers = "on"
        _map_user(worker.store, "bob", "222")
        worker.clickup = FakeCU({"sub1": [
            _comment("s1", "/proceed I say go", user_id="999", username="rando"),
            _comment("s2", "/proceed approved", user_id="222", username="bob-cu", age=-3700),
        ]})
        job = _park_feature(worker, "feat-at3", stage=5, dev_dri="222")
        # poll 1: the rando's comment is refused (first sighting) and the scan stops
        asyncio.run(worker._scan_verbs(job, "sub1", use_marker=False, after=0))
        assert worker.store.get("feat-at3")["status"] == "awaiting_input"
        # poll 2: dedupe-hit SKIPS s1; bob's verb lands
        asyncio.run(worker._scan_verbs(worker.store.get("feat-at3"), "sub1",
                                       use_marker=False, after=0))
        row = worker.store.get("feat-at3")
        assert row["status"] == "queued" and row["stage"] == 6
        assert worker.store.guidance_for("feat-at3")[-1]["via"] == "clickup:bob"

    def test_auto_with_any_mapping_is_strict(self, worker):
        _map_user(worker.store, "jane", "333")  # someone is mapped -> auto = strict
        worker.clickup = FakeCU({"cu1": [_comment("c1", "/proceed go",
                                                  user_id="999", username="rando")]})
        job = _park_feature(worker, "feat-at4")
        asyncio.run(worker._scan_verbs(job, "cu1", use_marker=True))
        assert worker.store.get("feat-at4")["status"] == "awaiting_input"

    def test_v1_verbs_are_attributed_too(self, worker):
        """The refusal sits in _scan_verbs before answer_job — sentry/task
        verbs get the same treatment (the audit hole closes everywhere)."""
        worker.settings.require_attributed_answers = "on"
        worker.clickup = FakeCU({"cut": [_comment("v1", "/proceed fix it",
                                                  user_id="999", username="rando")]})
        worker.intake_task("task-at1", title="T", project="web", request="r",
                           clickup_task_id="cut")
        worker.store.set_status("task-at1", "awaiting_input")
        job = worker.store.get("task-at1")
        asyncio.run(worker._scan_verbs(job, "cut", use_marker=True))
        row = worker.store.get("task-at1")
        assert row["status"] == "awaiting_input" and row["phase"] == 1
        assert [e["kind"] for e in worker.store.gate_events_for("task-at1")] \
            == ["refused_unattributed"]


class TestPermissiveModes:
    def test_off_preserves_todays_behavior(self, worker):
        worker.settings.require_attributed_answers = "off"
        worker.clickup = FakeCU({"cu1": [_comment("c1", "/proceed go",
                                                  user_id="999", username="rando")]})
        job = _park_feature(worker, "feat-at5")
        asyncio.run(worker._scan_verbs(job, "cu1", use_marker=True))
        row = worker.store.get("feat-at5")
        assert row["status"] == "queued" and row["stage"] == 4
        # ...but via still records WHO, even unmapped (the audit hole closes)
        assert worker.store.guidance_for("feat-at5")[-1]["via"] == "clickup:rando#999"

    def test_auto_with_zero_mappings_is_permissive(self, worker):
        worker.clickup = FakeCU({"cu1": [_comment("c1", "/proceed go",
                                                  user_id="999", username="rando")]})
        job = _park_feature(worker, "feat-at6")
        asyncio.run(worker._scan_verbs(job, "cu1", use_marker=True))
        assert worker.store.get("feat-at6")["status"] == "queued"

    def test_legacy_comment_shape_counts_as_unattributed(self, worker):
        """Comments without user keys (old fakes / odd payloads) never crash;
        they are anonymous — applied under 'off', refused under 'on'."""
        worker.clickup = FakeCU({"cu1": [_comment("c1", "/proceed go")]})
        job = _park_feature(worker, "feat-at7")
        asyncio.run(worker._scan_verbs(job, "cu1", use_marker=True))
        assert worker.store.get("feat-at7")["status"] == "queued"  # auto, no mappings
        assert worker.store.guidance_for("feat-at7")[-1]["via"] == "clickup:unknown#?"

        worker.settings.require_attributed_answers = "on"
        worker.clickup = FakeCU({"cu1": [_comment("c2", "/proceed go")]})
        job = _park_feature(worker, "feat-at8")
        asyncio.run(worker._scan_verbs(job, "cu1", use_marker=True))
        assert worker.store.get("feat-at8")["status"] == "awaiting_input"


class TestWrongRoleOverClickUp:
    def test_non_owner_verb_is_refused_with_ownership_reply(self, worker):
        _map_user(worker.store, "jane", "333")
        _map_user(worker.store, "bob", "222")
        worker.clickup = FakeCU({"cu1": [_comment("c1", "/proceed ship it",
                                                  user_id="333", username="jane-cu")]})
        # P5 is a dev gate; bob (222) is the dev DRI — jane doesn't own it
        job = _park_feature(worker, "feat-at9", stage=5, dev_dri="222")
        asyncio.run(worker._scan_verbs(job, "cu1", use_marker=True))
        row = worker.store.get("feat-at9")
        assert row["status"] == "awaiting_input"
        events = worker.store.gate_events_for("feat-at9")
        assert [e["kind"] for e in events] == ["refused_wrong_role"]
        replies = [t for _, t in worker.clickup.posted if "Not applied" in t]
        assert len(replies) == 1 and "dev gate" in replies[0] and "bob" in replies[0]
        # repeat poll (marker cleared to simulate crash): dedupe -> no second reply
        worker.store.set_fields("feat-at9", comment_marker="")
        asyncio.run(worker._scan_verbs(worker.store.get("feat-at9"), "cu1",
                                       use_marker=False, after=0))
        assert len([t for _, t in worker.clickup.posted if "Not applied" in t]) == 1

    def test_owner_verb_applies(self, worker):
        _map_user(worker.store, "bob", "222")
        worker.clickup = FakeCU({"cu1": [_comment("c1", "/proceed approved",
                                                  user_id="222", username="bob-cu")]})
        job = _park_feature(worker, "feat-at10", stage=5, dev_dri="222")
        asyncio.run(worker._scan_verbs(job, "cu1", use_marker=True))
        row = worker.store.get("feat-at10")
        assert row["status"] == "queued" and row["stage"] == 6

    def test_off_mode_with_dris_stays_fail_closed(self, worker):
        """Documented consequence (ENGINE.md §2): with attribution OFF but DRIs
        set, an unresolved ClickUp commenter can never own the gate — the verb
        is refused with the ownership reply. A3 implies A1 over ClickUp."""
        worker.settings.require_attributed_answers = "off"
        worker.clickup = FakeCU({"cu1": [_comment("c1", "/proceed go",
                                                  user_id="999", username="rando")]})
        job = _park_feature(worker, "feat-at11", stage=5, dev_dri="4242")
        asyncio.run(worker._scan_verbs(job, "cu1", use_marker=True))
        row = worker.store.get("feat-at11")
        assert row["status"] == "awaiting_input"
        assert [e["kind"] for e in worker.store.gate_events_for("feat-at11")] \
            == ["refused_wrong_role"]

"""Epic A: the one gate-ownership resolver (app/roles.py) both channels use.
Enforcement keys EXCLUSIVELY on the explicit DRI columns — the legacy `owner`
column resolves display/assignment only (enforce=False), so an upgraded
instance's in-flight jobs never start refusing gate answers."""

import json

import pytest

from app import roles
from app.config import DEFAULT_STAGE_ROLE_MAP, validate_stage_role_map


def _feature(store, job_id="feat-r1", stage=3, **fields):
    store.feature_intake(job_id, title="F", project="web", stage=stage, **fields)
    return store.get(job_id)


class TestValidateStageRoleMap:
    def test_valid_partial_map(self):
        assert validate_stage_role_map({"7": "founder"}) == {"7": "founder"}
        assert validate_stage_role_map({}) == {}

    def test_normalizes_case_and_types(self):
        assert validate_stage_role_map({7: "Founder"}) == {"7": "founder"}

    def test_rejects_bad_stage(self):
        with pytest.raises(ValueError):
            validate_stage_role_map({"10": "dev"})
        with pytest.raises(ValueError):
            validate_stage_role_map({"x": "dev"})

    def test_rejects_bad_role(self):
        with pytest.raises(ValueError):
            validate_stage_role_map({"3": "manager"})

    def test_rejects_non_dict(self):
        with pytest.raises(ValueError):
            validate_stage_role_map(["0", "dev"])


class TestRoleForStage:
    def test_default_ladder(self, settings):
        assert roles.role_for_stage(settings, None, 0) == "founder"
        assert roles.role_for_stage(settings, None, 1) == "founder"
        for s in range(2, 9):
            assert roles.role_for_stage(settings, None, s) == "dev"
        assert roles.role_for_stage(settings, None, 9) == "founder"

    def test_instance_override(self, settings):
        settings.stage_role_map = json.dumps({"7": "founder"})
        assert roles.role_for_stage(settings, None, 7) == "founder"
        assert roles.role_for_stage(settings, None, 5) == "dev"  # partial map merges

    def test_workspace_override_beats_instance(self, settings):
        settings.stage_role_map = json.dumps({"7": "founder"})
        ws = {"stage_role_map": json.dumps({"7": "dev", "2": "founder"})}
        assert roles.role_for_stage(settings, ws, 7) == "dev"
        assert roles.role_for_stage(settings, ws, 2) == "founder"

    def test_malformed_stored_map_falls_through(self, settings):
        ws = {"stage_role_map": "{not json"}
        assert roles.role_for_stage(settings, ws, 0) == "founder"
        settings.stage_role_map = "[1,2]"  # wrong shape
        assert roles.role_for_stage(settings, None, 5) == "dev"

    def test_default_map_covers_all_stages(self):
        assert set(DEFAULT_STAGE_ROLE_MAP) == {str(i) for i in range(10)}


class TestGateOwner:
    def test_no_dri_of_any_kind_is_none(self, settings, store):
        job = _feature(store, stage=3)
        assert roles.gate_owner(store, settings, None, job) is None

    def test_non_feature_is_none(self, settings, store):
        store.insert("task-r1", source="manual", kind="task", project="web")
        store.set_fields("task-r1", owner="4242")
        assert roles.gate_owner(store, settings, None, store.get("task-r1")) is None

    def test_role_slot_wins(self, settings, store):
        job = _feature(store, stage=0, founder_dri="111", dev_dri="222")
        o = roles.gate_owner(store, settings, None, job)
        assert (o.role, o.value, o.enforce) == ("founder", "111", True)
        job = _feature(store, "feat-r2", stage=5, founder_dri="111", dev_dri="222")
        o = roles.gate_owner(store, settings, None, job)
        assert (o.role, o.value) == ("dev", "222")

    def test_other_dri_fallback(self, settings, store):
        # dev stage, only a founder DRI recorded -> the founder covers it
        job = _feature(store, stage=5, founder_dri="111")
        o = roles.gate_owner(store, settings, None, job)
        assert (o.role, o.value, o.enforce) == ("dev", "111", True)

    def test_legacy_owner_resolves_but_never_enforces(self, settings, store):
        """Blocker 1: a pre-upgrade job with only `owner` set must resolve for
        assignment/display, with enforcement OFF."""
        job = _feature(store, stage=5, owner="4242")
        o = roles.gate_owner(store, settings, None, job)
        assert o is not None
        assert o.enforce is False
        assert o.clickup_id == "4242"
        assert "4242" in o.display
        # and ANY actor may answer such a gate
        assert roles.actor_is_owner(o, None) is False  # matching itself still strict

    def test_mapped_user_display(self, settings, store):
        store.user_create("jane", "hash")
        store.user_set("jane", clickup_user_id="333")
        job = _feature(store, stage=0, founder_dri="333")
        o = roles.gate_owner(store, settings, None, job)
        assert o.display == "jane"
        assert o.clickup_id == "333"
        assert o.user["username"] == "jane"

    def test_username_dri(self, settings, store):
        store.user_create("jane", "hash")
        store.user_set("jane", clickup_user_id="333")
        job = _feature(store, stage=0, founder_dri="jane")
        o = roles.gate_owner(store, settings, None, job)
        assert o.display == "jane"
        assert o.clickup_id == "333"  # resolved through the mapping


class TestActorIsOwner:
    def _owner(self, **kw):
        base = dict(role="dev", value="4242", clickup_id="4242", user=None,
                    display="ClickUp user 4242", enforce=True)
        base.update(kw)
        return roles.GateOwner(**base)

    def test_none_owner_matches_everyone(self):
        assert roles.actor_is_owner(None, None) is True
        assert roles.actor_is_owner(None, {"username": "x"}) is True

    def test_clickup_id_match(self):
        o = self._owner()
        assert roles.actor_is_owner(o, {"username": "jane", "clickup_user_id": "4242"})
        assert not roles.actor_is_owner(o, {"username": "jane", "clickup_user_id": "9"})
        assert not roles.actor_is_owner(o, None)

    def test_username_match_direct_and_via_mapping(self):
        o = self._owner(value="jane", clickup_id="")
        assert roles.actor_is_owner(o, {"username": "jane"})
        o2 = self._owner(user={"username": "jane", "clickup_user_id": "4242"})
        assert roles.actor_is_owner(o2, {"username": "jane"})

    def test_empty_compare_safety(self):
        # empty clickup ids on both sides must never match
        o = self._owner(clickup_id="")
        assert not roles.actor_is_owner(o, {"username": "someone", "clickup_user_id": ""})


class TestOtherDri:
    def test_other_dri(self):
        job = {"founder_dri": "1", "dev_dri": "2"}
        assert roles.other_dri(job, "founder") == "2"
        assert roles.other_dri(job, "dev") == "1"
        assert roles.other_dri({"founder_dri": "", "dev_dri": ""}, "dev") == ""


class TestAttributionRequired:
    def test_instance_modes(self, settings, store):
        settings.require_attributed_answers = "on"
        assert roles.attribution_required(settings, None, store) is True
        settings.require_attributed_answers = "off"
        assert roles.attribution_required(settings, None, store) is False

    def test_auto_follows_mappings(self, settings, store):
        settings.require_attributed_answers = "auto"
        assert roles.attribution_required(settings, None, store) is False
        store.user_create("jane", "hash")
        store.user_set("jane", clickup_user_id="333")
        assert roles.attribution_required(settings, None, store) is True
        store.user_set("jane", disabled=1)  # disabled mappings don't count
        assert roles.attribution_required(settings, None, store) is False

    def test_workspace_value_wins(self, settings, store):
        settings.require_attributed_answers = "on"
        assert roles.attribution_required(settings, {"require_attributed_answers": "off"},
                                          store) is False
        settings.require_attributed_answers = "off"
        assert roles.attribution_required(settings, {"require_attributed_answers": "on"},
                                          store) is True

"""Epic D1: the people profile layer over users — validation, routing
defaults at intake, and the prompt ownership block. Enforcement invariants:
profiles only FILL empty DRI columns at intake; roles.gate_owner still keys
exclusively on the job's own DRI columns."""

import asyncio
import base64
import json

import pytest

from app import people


AUTH = {"Authorization": "Basic " + base64.b64encode(b"gumo:test").decode()}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test")

    from app import config

    config.get_settings.cache_clear()
    import importlib

    from app import main as main_module
    from fastapi.testclient import TestClient

    importlib.reload(main_module)
    with TestClient(main_module.app) as c:
        yield c
    config.get_settings.cache_clear()


def _svc(store, settings):
    from app.workspaces import WorkspaceService

    svc = WorkspaceService(store, settings)
    svc.ensure_default()
    return svc


def _profile_user(store, username, person_role="", areas=None, member_of=None):
    user = store.user_create(username, "hash")
    store.person_set(user["id"], person_role=person_role,
                     areas=json.dumps(areas or []), authority="[]", notes="")
    if member_of is not None:
        store.workspace_member_set(member_of, user["id"], True)
    return user


class TestValidateProfile:
    def test_valid_roundtrip(self):
        out = people.validate_profile({
            "person_role": "Founder",
            "areas": [{"kind": "workspace", "value": "app"},
                      {"kind": "repo", "value": "web"}],
            "authority": ["pricing", "api design"],
            "notes": "hi",
        })
        assert out["person_role"] == "founder"
        assert out["areas"] == [{"kind": "workspace", "value": "app"},
                                {"kind": "repo", "value": "web"}]
        assert out["authority"] == ["pricing", "api design"]

    def test_bad_role_rejected(self):
        with pytest.raises(ValueError):
            people.validate_profile({"person_role": "ceo"})

    def test_bad_areas_rejected(self):
        with pytest.raises(ValueError):
            people.validate_profile({"areas": [{"kind": "planet", "value": "x"}]})
        with pytest.raises(ValueError):
            people.validate_profile({"areas": [{"kind": "repo", "value": ""}]})
        with pytest.raises(ValueError):
            people.validate_profile({"areas": "not json ["})
        with pytest.raises(ValueError):
            people.validate_profile(
                {"areas": [{"kind": "repo", "value": "x"}] * 33})

    def test_authority_both_caps(self):
        # amendment 12: <=16 entries AND <=48 chars each, single-lined
        with pytest.raises(ValueError):
            people.validate_profile({"authority": ["a"] * 17})
        out = people.validate_profile({"authority": ["x" * 100, "a\nb"]})
        assert len(out["authority"][0]) == 48
        assert out["authority"][1] == "a b"

    def test_notes_cap(self):
        with pytest.raises(ValueError):
            people.validate_profile({"notes": "x" * 2001})


class TestDefaultDris:
    def test_exact_match_fills(self, store, settings):
        svc = _svc(store, settings)
        ws = store.workspace_get_by_slug("default")
        _profile_user(store, "fiona", "founder",
                      [{"kind": "workspace", "value": "default"}], ws["id"])
        _profile_user(store, "devon", "dev",
                      [{"kind": "repo", "value": "web"}], ws["id"])
        assert people.default_dris(store, ws, "web") == ("fiona", "devon")

    def test_ambiguity_fills_nothing(self, store, settings):
        svc = _svc(store, settings)
        ws = store.workspace_get_by_slug("default")
        _profile_user(store, "d1", "dev", [{"kind": "repo", "value": "web"}], ws["id"])
        _profile_user(store, "d2", "dev", [{"kind": "workspace", "value": "default"}], ws["id"])
        assert people.default_dris(store, ws, "web") == ("", "")

    def test_empty_areas_cover_nothing(self, store, settings):
        svc = _svc(store, settings)
        ws = store.workspace_get_by_slug("default")
        _profile_user(store, "d1", "dev", [], ws["id"])
        assert people.default_dris(store, ws, "web") == ("", "")

    def test_non_member_never_routed(self, store, settings):
        """A profile may claim a workspace its user cannot access — appointing
        a non-member would wedge the gate behind membership 404s."""
        svc = _svc(store, settings)
        ws = store.workspace_get_by_slug("default")
        _profile_user(store, "outsider", "dev",
                      [{"kind": "repo", "value": "web"}], member_of=None)
        assert people.default_dris(store, ws, "web") == ("", "")

    def test_disabled_user_excluded(self, store, settings):
        svc = _svc(store, settings)
        ws = store.workspace_get_by_slug("default")
        _profile_user(store, "d1", "dev", [{"kind": "repo", "value": "web"}], ws["id"])
        store.user_set("d1", disabled=1)
        assert people.default_dris(store, ws, "web") == ("", "")

    def test_product_and_design_map_to_no_slot(self, store, settings):
        svc = _svc(store, settings)
        ws = store.workspace_get_by_slug("default")
        _profile_user(store, "pm", "product", [{"kind": "repo", "value": "web"}], ws["id"])
        _profile_user(store, "des", "design", [{"kind": "repo", "value": "web"}], ws["id"])
        assert people.default_dris(store, ws, "web") == ("", "")

    def test_no_workspace_fails_closed(self, store, settings):
        _profile_user(store, "d1", "dev", [{"kind": "repo", "value": "web"}])
        assert people.default_dris(store, None, "web") == ("", "")


class TestIntakeFill:
    def _wire(self, worker):
        worker.workspaces = _svc(worker.store, worker.settings)
        ws = worker.store.workspace_get_by_slug("default")
        _profile_user(worker.store, "fiona", "founder",
                      [{"kind": "workspace", "value": "default"}], ws["id"])
        _profile_user(worker.store, "devon", "dev",
                      [{"kind": "repo", "value": "web"}], ws["id"])
        return ws

    def test_fills_only_empty_slots(self, worker):
        self._wire(worker)
        worker.intake_feature("feat-p1", title="F", project="web", request="r",
                              dev_dri="explicit-dev")
        job = worker.store.get("feat-p1")
        assert job["founder_dri"] == "fiona"     # empty slot filled
        assert job["dev_dri"] == "explicit-dev"  # explicit submission wins
        assert job["owner"] == "explicit-dev"    # legacy alias computed AFTER fill

    def test_fills_both_when_empty(self, worker):
        self._wire(worker)
        worker.intake_feature("feat-p2", title="F", project="web", request="r")
        job = worker.store.get("feat-p2")
        assert (job["founder_dri"], job["dev_dri"]) == ("fiona", "devon")
        assert job["owner"] == "devon"

    def test_flag_off_fills_nothing(self, worker):
        self._wire(worker)
        worker.settings.people_routing_defaults = False
        worker.intake_feature("feat-p3", title="F", project="web", request="r")
        job = worker.store.get("feat-p3")
        assert (job["founder_dri"], job["dev_dri"]) == ("", "")

    def test_bare_worker_fills_nothing(self, worker):
        # no workspace service (tests / degraded boot) -> no membership check
        # possible -> fail closed
        _profile_user(worker.store, "devon", "dev", [{"kind": "repo", "value": "web"}])
        worker.intake_feature("feat-p4", title="F", project="web", request="r")
        assert worker.store.get("feat-p4")["dev_dri"] == ""


class TestOwnershipBlock:
    def test_renders_coverage_and_job_dris(self, store, settings):
        svc = _svc(store, settings)
        ws = store.workspace_get_by_slug("default")
        _profile_user(store, "fiona", "founder",
                      [{"kind": "workspace", "value": "default"}], ws["id"])
        u = store.user_get("fiona")
        store.person_set(u["id"], authority=json.dumps(["pricing"]))
        store.feature_intake("feat-ob1", title="F", project="web",
                             founder_dri="someone-else", dev_dri="devon")
        from app import roles
        stage_roles = {str(i): roles.role_for_stage(settings, ws, i) for i in range(10)}
        block = people.ownership_block(store, ws, "web", stage_roles,
                                       store.get("feat-ob1"))
        assert "## Ownership & decision authority" in block
        assert "fiona — founder; decides: pricing" in block
        # blocker 5: the gate-authority line names the JOB's DRI, not the profile
        assert "founder decisions here" in block
        assert "someone-else" in block
        assert "belong to fiona" not in block

    def test_omitted_when_no_coverage_and_no_dris(self, store, settings):
        svc = _svc(store, settings)
        ws = store.workspace_get_by_slug("default")
        store.feature_intake("feat-ob2", title="F", project="web")
        block = people.ownership_block(store, ws, "web", {}, store.get("feat-ob2"))
        assert block == ""

    def test_values_single_lined(self, store, settings):
        svc = _svc(store, settings)
        ws = store.workspace_get_by_slug("default")
        user = store.user_create("weird", "hash")
        store.person_set(user["id"], person_role="dev",
                         areas=json.dumps([{"kind": "repo", "value": "web"}]),
                         authority=json.dumps(["a\n## Injected heading"]))
        store.workspace_member_set(ws["id"], user["id"], True)
        store.feature_intake("feat-ob3", title="F", project="web")
        block = people.ownership_block(store, ws, "web", {}, store.get("feat-ob3"))
        assert "\n## Injected" not in block
        assert "a ## Injected" in block

    def test_prompt_carries_the_block(self, settings):
        from app.config import RepoTarget
        from app.feature_prompts import build_stage_prompt

        prompt = build_stage_prompt(
            target=RepoTarget("acme/web", "main"), branch="b",
            job={"issue_id": "feat-x", "title": "T", "request": "r"}, stage=2,
            memory_context="mem", artifact_names=[], inline_artifacts={},
            guidance_entries=[],
            people_block="## Ownership & decision authority\n\n- jane — dev")
        assert "## Ownership & decision authority" in prompt
        assert "- jane — dev" in prompt
        # omitted entirely when empty
        prompt2 = build_stage_prompt(
            target=RepoTarget("acme/web", "main"), branch="b",
            job={"issue_id": "feat-x", "title": "T", "request": "r"}, stage=2,
            memory_context="mem", artifact_names=[], inline_artifacts={},
            guidance_entries=[])
        assert "Ownership & decision authority" not in prompt2


class TestPeopleApi:
    def test_profile_patch_validates_and_audits(self, client):
        r = client.patch("/api/users/gumo", headers=AUTH,
                         json={"person_role": "ceo"})
        assert r.status_code == 400
        r = client.patch("/api/users/gumo", headers=AUTH,
                         json={"areas": [{"kind": "nope", "value": "x"}]})
        assert r.status_code == 400
        # nothing changed, nothing audited
        r = client.get("/api/users", headers=AUTH)
        u = next(x for x in r.json() if x["username"] == "gumo")
        assert u["person_role"] == "" and u["areas"] == []

        r = client.patch("/api/users/gumo", headers=AUTH, json={
            "person_role": "founder",
            "areas": [{"kind": "workspace", "value": "default"}],
            "authority": ["pricing"]})
        assert r.status_code == 200
        r = client.get("/api/users", headers=AUTH)
        u = next(x for x in r.json() if x["username"] == "gumo")
        assert u["person_role"] == "founder"
        assert u["areas"] == [{"kind": "workspace", "value": "default"}]
        assert u["authority"] == ["pricing"]

        events = client.app.state.store.admin_events_recent()
        prof = [e for e in events if e["kind"] == "people_profile"]
        assert len(prof) == 1
        assert prof[0]["target"] == "gumo"
        assert "founder" in prof[0]["detail"]
        assert prof[0]["actor"] == "dashboard:gumo"

        # unchanged re-submit audits nothing
        client.patch("/api/users/gumo", headers=AUTH, json={
            "person_role": "founder",
            "areas": [{"kind": "workspace", "value": "default"}],
            "authority": ["pricing"]})
        events = client.app.state.store.admin_events_recent()
        assert len([e for e in events if e["kind"] == "people_profile"]) == 1

    def test_people_endpoint_scoped_for_members(self, client):
        store = client.app.state.store
        # profile the admin over the default workspace
        client.patch("/api/users/gumo", headers=AUTH, json={
            "person_role": "founder",
            "areas": [{"kind": "workspace", "value": "default"},
                      {"kind": "area", "value": "billing"}]})
        # a member with NO workspace: sees nobody
        client.post("/api/users", headers=AUTH,
                    json={"username": "meg", "password": "longenough",
                          "role": "member"})
        store.user_set("meg", must_change_pw=0)
        meg_auth = {"Authorization": "Basic "
                    + base64.b64encode(b"meg:longenough").decode()}
        r = client.get("/api/people", headers=meg_auth)
        assert r.status_code == 200
        assert r.json() == []
        # membership grants visibility — and only intersecting area entries
        ws = store.workspace_get_by_slug("default")
        store.workspace_member_set(ws["id"], store.user_get("meg")["id"], True)
        r = client.get("/api/people", headers=meg_auth)
        names = {p["username"] for p in r.json()}
        assert "gumo" in names
        entry = next(p for p in r.json() if p["username"] == "gumo")
        assert {"kind": "workspace", "value": "default"} in entry["areas"]
        assert all(a["kind"] != "area" for a in entry["areas"])
        assert "notes" not in entry
        # admins see everyone (full areas)
        r = client.get("/api/people", headers=AUTH)
        entry = next(p for p in r.json() if p["username"] == "gumo")
        assert {"kind": "area", "value": "billing"} in entry["areas"]

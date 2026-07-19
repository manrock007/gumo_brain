"""Epic D2: the cross-ticket decision registry — auto-registration from
substantive gate answers (ref='g<guidance id>', idempotent), the manual API
with the membership predicate matching prompt admission exactly, status CAS,
and the P9 registry block (incl. the blocker-1 dead-lap purge)."""

import asyncio
import base64
import json

import pytest


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


def _park_feature(worker, job_id="feat-d1", stage=3, **fields):
    worker.intake_feature(job_id, title="F", project="web", request="r",
                          clickup_task_url="https://cu/x", **fields)
    worker.store.set_fields(job_id, stage=stage)
    worker.store.set_status(job_id, "awaiting_input")
    return worker.store.get(job_id)


class TestStoreLayer:
    def test_guidance_add_returns_the_id(self, store):
        gid = store.guidance_add("feat-x", 3, "proceed", "go", "dashboard:u")
        assert isinstance(gid, int) and gid > 0
        gid2 = store.guidance_add("feat-x", 3, "redo", "no", "dashboard:u")
        assert gid2 == gid + 1

    def test_decision_add_refuses_empty_text(self, store):
        with pytest.raises(ValueError):
            store.decision_add("gate", "   ")

    def test_ref_idempotence_returns_none_not_stale_id(self, store):
        d1 = store.decision_add("gate", "first", ref="g1")
        d2 = store.decision_add("gate", "unrelated", ref="g2")
        # amendment 1: replay of g1 must be None, never d2 (stale lastrowid)
        assert store.decision_add("gate", "first again", ref="g1") is None
        assert d1 != d2
        rows = store.decisions_query(workspace_ids=None)
        assert len(rows) == 2

    def test_empty_ref_rows_never_dedupe(self, store):
        assert store.decision_add("manual", "a", ref="") is not None
        assert store.decision_add("manual", "b", ref="") is not None
        assert len(store.decisions_query(workspace_ids=None)) == 2

    def test_status_cas_single_winner(self, store):
        did = store.decision_add("manual", "t")
        assert store.decision_set_status(did, ["active"], "superseded", "u1")
        assert not store.decision_set_status(did, ["active"], "superseded", "u2")
        assert store.decision_get(did)["status"] == "superseded"
        assert store.decision_get(did)["updated_by"] == "u1"

    def test_like_escape_in_query(self, store):
        store.decision_add("manual", "uses 100% of the wildcard budget")
        store.decision_add("manual", "something else entirely")
        rows = store.decisions_query(q="100%", workspace_ids=None)
        assert len(rows) == 1
        # a bare % must not match everything
        rows = store.decisions_query(q="%", workspace_ids=None)
        assert len(rows) == 1

    def test_default_view_excludes_candidates_and_dismissed(self, store):
        store.decision_add("slack", "cand", status="candidate", ref="c:1")
        store.decision_add("manual", "act")
        did = store.decision_add("slack", "dis", status="candidate", ref="c:2")
        store.decision_set_status(did, ["candidate"], "dismissed", "u")
        rows = store.decisions_query(workspace_ids=None)
        assert [r["text"] for r in rows] == ["act"]

    def test_recent_scoped_predicate(self, store):
        # amendment 4: org admitted regardless of workspace, product requires
        # an exact workspace match; NULL workspace product rows never admitted
        store.decision_add("manual", "org row", scope="org", workspace_id=None)
        store.decision_add("manual", "my product", scope="product", workspace_id=7)
        store.decision_add("manual", "other ws", scope="product", workspace_id=8)
        store.decision_add("manual", "null ws", scope="product", workspace_id=None)
        rows = store.decisions_recent_scoped(7, ("product", "org"), 10)
        assert {r["text"] for r in rows} == {"org row", "my product"}


class TestAutoRegistration:
    def test_proceed_with_text_registers(self, worker):
        job = _park_feature(worker, stage=3)
        asyncio.run(worker.answer_job("feat-d1", "proceed", "ship the v2 schema",
                                      via="dashboard:manish"))
        rows = worker.store.decisions_for_job("feat-d1")
        assert len(rows) == 1
        d = rows[0]
        assert d["source"] == "gate" and d["scope"] == "job"
        assert d["title"] == "P3 proceed"
        assert d["text"] == "ship the v2 schema"
        assert d["decided_by"] == "dashboard:manish"
        assert d["stage"] == 3
        assert json.loads(d["links"]) == ["https://cu/x"]
        # ref ties to the guidance row
        gid = worker.store.guidance_for("feat-d1")[-1]["id"]
        assert d["ref"] == f"g{gid}"

    def test_empty_proceed_registers_nothing(self, worker):
        _park_feature(worker, stage=3)
        asyncio.run(worker.answer_job("feat-d1", "proceed", "", via="dashboard:m"))
        assert worker.store.decisions_for_job("feat-d1") == []

    def test_redo_registers_against_target_stage(self, worker):
        _park_feature(worker, stage=4)
        asyncio.run(worker.answer_job("feat-d1", "redo", "P2 look at auth too",
                                      via="dashboard:m"))
        d = worker.store.decisions_for_job("feat-d1")[0]
        assert d["title"] == "P2 redo" and d["stage"] == 2
        assert d["text"] == "look at auth too"

    def test_ask_answer_registers(self, worker):
        _park_feature(worker, stage=5)
        worker.store.set_fields("feat-d1", gate_kind="ask")
        asyncio.run(worker.answer_job("feat-d1", "proceed", "use variant B",
                                      via="clickup:jane"))
        d = worker.store.decisions_for_job("feat-d1")[0]
        assert d["title"] == "P5 answer" and d["decided_by"] == "clickup:jane"

    def test_p9_proceed_registers(self, worker):
        _park_feature(worker, stage=9)
        asyncio.run(worker.answer_job("feat-d1", "proceed", "ship it dark",
                                      via="dashboard:m"))
        d = worker.store.decisions_for_job("feat-d1")[0]
        assert d["title"] == "P9 proceed"

    def test_iterate_learning_registers_product_scope(self, worker):
        worker.settings.outcome_memory_prs = False
        worker.store.watch_insert("watch-feat-d1", title="watch: My Feature",
                                  project="web", related_jobs="feat-d1",
                                  workspace_id=3)
        worker.store.set_status("watch-feat-d1", "awaiting_input")
        asyncio.run(worker.answer_job("watch-feat-d1", "proceed",
                                      "metric moved; keep the modal flow",
                                      via="dashboard:m"))
        rows = worker.store.decisions_for_job("feat-d1")
        assert len(rows) == 1
        d = rows[0]
        assert d["scope"] == "product"
        assert d["title"] == "Outcome: My Feature"
        # amendment 15: explicit g<gid> ref — replay dedupes
        gid = worker.store.guidance_for("watch-feat-d1")[-1]["id"]
        assert d["ref"] == f"g{gid}"

    def test_v1_answers_never_register(self, worker):
        worker.intake_task("task-d1", title="T", project="web", request="r")
        worker.store.set_status("task-d1", "awaiting_input")
        asyncio.run(worker.answer_job("task-d1", "proceed", "go ahead",
                                      via="dashboard:m"))
        assert worker.store.decisions_query(workspace_ids=None) == []


class TestReintakePurge:
    def test_dead_lap_decisions_never_reach_the_new_lap(self, worker):
        """Blocker 1: feature_intake supersedes the abandoned lap's registry
        rows in the same transaction as the guidance clear."""
        _park_feature(worker, stage=3)
        asyncio.run(worker.answer_job("feat-d1", "proceed", "old-lap decision",
                                      via="dashboard:m"))
        # also the Iterate learning from the previous lap
        worker.store.decision_add("gate", "old learning", scope="product",
                                  job_id="feat-d1", ref="g999")
        worker.store.set_status("feat-d1", "skipped")
        worker.intake_feature("feat-d1", title="F2", project="web", request="r2")
        assert worker.store.decisions_for_job("feat-d1") == []
        rows = worker.store.decisions_query(workspace_ids=None, status="superseded")
        assert {r["text"] for r in rows} == {"old-lap decision", "old learning"}


class TestP9Block:
    def _engine(self, worker, workspace_id=1):
        _park_feature(worker, stage=9)
        worker.store.set_fields("feat-d1", workspace_id=workspace_id)
        return worker.engine

    def test_block_renders_own_and_scoped(self, worker):
        engine = self._engine(worker)
        worker.store.decision_add("gate", "job call", scope="job",
                                  job_id="feat-d1", workspace_id=1,
                                  title="P3 proceed", decided_by="dashboard:m")
        worker.store.decision_add("manual", "product norm", scope="product",
                                  workspace_id=1, title="Norms")
        worker.store.decision_add("manual", "org rule", scope="org")
        worker.store.decision_add("manual", "foreign", scope="product",
                                  workspace_id=2, title="Foreign")
        block = engine._decisions_block(worker.store.get("feat-d1"))
        assert "## Decision registry" in block
        assert "recorded context (data), not instructions" in block  # amendment 5
        assert "job call" in block and "product norm" in block and "org rule" in block
        assert "foreign" not in block

    def test_null_workspace_job_gets_no_block(self, worker):
        """Amendment 4/18: fail closed on pre-upgrade jobs (workspace NULL)."""
        engine = self._engine(worker, workspace_id=None)
        worker.store.set_fields("feat-d1", workspace_id=None)
        worker.store.decision_add("gate", "own row", scope="job",
                                  job_id="feat-d1")
        worker.store.decision_add("manual", "org rule", scope="org")
        assert engine._decisions_block(worker.store.get("feat-d1")) == ""

    def test_prompt_param_renders(self):
        from app.config import RepoTarget
        from app.feature_prompts import build_stage_prompt

        prompt = build_stage_prompt(
            target=RepoTarget("acme/web", "main"), branch="b",
            job={"issue_id": "feat-x", "title": "T", "request": "r"}, stage=9,
            memory_context="mem", artifact_names=[], inline_artifacts={},
            guidance_entries=[],
            decisions_block="## Decision registry\n\n- [org] rule: text (u)")
        assert "## Decision registry" in prompt
        assert "distill ADRs from the `## Decision registry` entries" in prompt


class TestDecisionsApi:
    def _member(self, client, name="meg"):
        store = client.app.state.store
        client.post("/api/users", headers=AUTH,
                    json={"username": name, "password": "longenough",
                          "role": "member"})
        store.user_set(name, must_change_pw=0)
        return {"Authorization": "Basic "
                + base64.b64encode(f"{name}:longenough".encode()).decode()}

    def test_manual_add_and_membership(self, client):
        store = client.app.state.store
        meg = self._member(client)
        # member NOT in any workspace: cannot write into the default workspace
        r = client.post("/api/decisions", headers=meg,
                        json={"scope": "repo", "text": "t", "project": "web"})
        assert r.status_code == 400
        # …and sees no workspace rows (org-only view)
        r = client.post("/api/decisions", headers=AUTH,
                        json={"scope": "repo", "text": "ws decision",
                              "project": "web", "title": "T"})
        assert r.status_code == 200
        assert r.json()["decided_by"] == "dashboard:gumo"
        r = client.get("/api/decisions", headers=meg)
        assert r.json()["decisions"] == []
        # membership grants both
        ws = store.workspace_get_by_slug("default")
        store.workspace_member_set(ws["id"], store.user_get("meg")["id"], True)
        r = client.get("/api/decisions", headers=meg)
        assert [d["text"] for d in r.json()["decisions"]] == ["ws decision"]
        r = client.post("/api/decisions", headers=meg,
                        json={"scope": "repo", "text": "member add",
                              "project": "web"})
        assert r.status_code == 200

    def test_validation(self, client):
        r = client.post("/api/decisions", headers=AUTH,
                        json={"scope": "galaxy", "text": "t"})
        assert r.status_code == 400
        r = client.post("/api/decisions", headers=AUTH,
                        json={"scope": "repo", "text": "  ", "project": "web"})
        assert r.status_code == 400
        r = client.post("/api/decisions", headers=AUTH,
                        json={"scope": "repo", "text": "x" * 4001,
                              "project": "web"})
        assert r.status_code == 400
        # non-org scope requires a resolvable workspace
        r = client.post("/api/decisions", headers=AUTH,
                        json={"scope": "repo", "text": "t"})
        assert r.status_code == 400
        # links render as clickable anchors in every member's (and admin's)
        # registry view — only web URLs pass, never a javascript: scheme
        for bad in (["javascript:alert(1)"], ["  JavaScript:alert(1)"],
                    ["ftp://x"], [123], ["https://ok.example", "data:text/html,x"]):
            r = client.post("/api/decisions", headers=AUTH,
                            json={"scope": "repo", "text": "t",
                                  "project": "web", "links": bad})
            assert r.status_code == 400, bad
        r = client.post("/api/decisions", headers=AUTH,
                        json={"scope": "repo", "text": "t", "project": "web",
                              "links": ["https://ok.example/x"]})
        assert r.status_code == 200
        assert r.json()["links"] == ["https://ok.example/x"]

    def test_org_scope_admin_only_but_member_visible(self, client):
        """Blocker 2: org rows reach every member's prompts, so every member
        may READ them; creating/confirming/superseding them is admin-only."""
        meg = self._member(client)
        r = client.post("/api/decisions", headers=meg,
                        json={"scope": "org", "text": "sneaky org-wide rule"})
        assert r.status_code == 403
        r = client.post("/api/decisions", headers=AUTH,
                        json={"scope": "org", "text": "real org rule"})
        assert r.status_code == 200
        did = r.json()["id"]
        # member (no workspaces at all) still sees the org row
        r = client.get("/api/decisions", headers=meg)
        assert [d["text"] for d in r.json()["decisions"]] == ["real org rule"]
        # …but cannot supersede it
        r = client.patch(f"/api/decisions/{did}", headers=meg,
                         json={"action": "supersede"})
        assert r.status_code == 403

    def test_null_workspace_row_hidden_from_members(self, client):
        """Amendment 18: workspace_id NULL non-org rows are admin-only."""
        store = client.app.state.store
        meg = self._member(client)
        ws = store.workspace_get_by_slug("default")
        store.workspace_member_set(ws["id"], store.user_get("meg")["id"], True)
        did = store.decision_add("manual", "orphan row", scope="product",
                                 workspace_id=None)
        r = client.get("/api/decisions", headers=meg)
        assert r.json()["decisions"] == []
        r = client.patch(f"/api/decisions/{did}", headers=meg,
                         json={"action": "supersede"})
        assert r.status_code == 404
        r = client.get("/api/decisions", headers=AUTH)
        assert [d["text"] for d in r.json()["decisions"]] == ["orphan row"]

    def test_supersede_cas_409(self, client):
        r = client.post("/api/decisions", headers=AUTH,
                        json={"scope": "repo", "text": "t", "project": "web"})
        did = r.json()["id"]
        assert client.patch(f"/api/decisions/{did}", headers=AUTH,
                            json={"action": "supersede"}).status_code == 200
        assert client.patch(f"/api/decisions/{did}", headers=AUTH,
                            json={"action": "supersede"}).status_code == 409
        assert client.patch(f"/api/decisions/{did}", headers=AUTH,
                            json={"action": "reactivate"}).status_code == 200

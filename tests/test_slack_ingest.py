"""Epic D3 (FLAG, off by default): Slack read ingestion — decision-shaped
messages become registry CANDIDATES parked for human confirmation, never
auto-committed. MockTransport throughout; no network."""

import asyncio
import base64
import json
import time

import httpx
import pytest

from app import slack_ingest


AUTH = {"Authorization": "Basic " + base64.b64encode(b"gumo:test").decode()}


def _msg(ts, text="", user="U1", reactions=None, **extra):
    m = {"ts": str(ts), "text": text, "user": user}
    if reactions is not None:
        m["reactions"] = reactions
    m.update(extra)
    return m


def _transport(state):
    def handler(request):
        state.setdefault("calls", []).append(str(request.url.path))
        assert "xoxb-test" not in str(request.url)  # token in header only
        if request.url.path.endswith("/conversations.history"):
            ch = request.url.params.get("channel")
            payload = state.get("history", {}).get(ch, [])
            if payload == "boom":
                return httpx.Response(500, text="server exploded")
            if isinstance(payload, dict):  # paginated fixture
                cursor = request.url.params.get("cursor") or ""
                page = payload.get(cursor, {"messages": []})
                return httpx.Response(200, json={
                    "ok": True, "messages": page["messages"],
                    "has_more": bool(page.get("next")),
                    "response_metadata": {"next_cursor": page.get("next") or ""}})
            return httpx.Response(200, json={"ok": True, "messages": payload,
                                             "has_more": False})
        if request.url.path.endswith("/chat.getPermalink"):
            return httpx.Response(200, json={"ok": True,
                                             "permalink": "https://slack/p/1"})
        return httpx.Response(404, text="no such method")
    return httpx.MockTransport(handler)


@pytest.fixture()
def swarm(worker):
    """Worker wired for ingestion: flag on, token set, one workspace with
    channel C1 allowlisted (cursor initialized in the past so fixtures with
    any ts are 'new')."""
    from app.workspaces import WorkspaceService

    worker.settings.slack_ingest_enabled = True
    worker.settings.slack_bot_token = "xoxb-test"
    svc = WorkspaceService(worker.store, worker.settings)
    svc.ensure_default()
    worker.workspaces = svc
    ws = worker.store.workspace_get_by_slug("default")
    svc.update(ws["id"], slack_channels=["C1"])
    worker.store.slack_cursor_set("C1", "1.000000")
    return worker, svc, ws


class TestShapes:
    def test_decision_shaped(self):
        assert slack_ingest.is_decision_shaped(_msg(1, "!decision use kafka"), "pushpin")
        assert slack_ingest.is_decision_shaped(
            _msg(1, "we choose kafka", reactions=[{"name": "pushpin"}]), "pushpin")
        assert not slack_ingest.is_decision_shaped(_msg(1, "plain chatter"), "pushpin")
        # bot/system messages never qualify
        assert not slack_ingest.is_decision_shaped(
            _msg(1, "!decision bot spam", subtype="bot_message"), "pushpin")
        assert not slack_ingest.is_decision_shaped(
            _msg(1, "!decision bot spam", bot_id="B1"), "pushpin")

    def test_candidate_fields(self):
        f = slack_ingest.candidate_fields(
            _msg(1, "!decision: adopt kafka\nbecause reasons", user="U42"))
        assert f["title"] == "adopt kafka"
        assert f["text"].startswith("!decision: adopt kafka")
        assert f["decided_by"] == "slack:U42"
        long = slack_ingest.candidate_fields(_msg(1, "!decision " + "x" * 5000))
        assert len(long["text"]) == slack_ingest.TEXT_CAP


class TestIngestLoop:
    def test_flag_off_makes_zero_http_calls(self, worker):
        state = {"history": {"C1": [_msg(2, "!decision x")]}}
        worker.settings.slack_ingest_enabled = False
        worker.settings.slack_bot_token = "xoxb-test"
        asyncio.run(worker._slack_ingest_once(transport=_transport(state)))
        assert state.get("calls", []) == []

    def test_no_token_makes_zero_http_calls(self, worker):
        state = {"history": {"C1": [_msg(2, "!decision x")]}}
        worker.settings.slack_ingest_enabled = True
        worker.settings.slack_bot_token = ""
        asyncio.run(worker._slack_ingest_once(transport=_transport(state)))
        assert state.get("calls", []) == []

    def test_candidates_once_across_two_passes(self, swarm):
        worker, svc, ws = swarm
        state = {"history": {"C1": [
            _msg("100.000001", "!decision adopt kafka", user="U1"),
            _msg("100.000002", "pin this one",
                 reactions=[{"name": "pushpin"}], user="U2"),
            _msg("100.000003", "ordinary chatter"),
        ]}}
        asyncio.run(worker._slack_ingest_once(transport=_transport(state)))
        cands = worker.store.decision_candidates(None)
        assert len(cands) == 2
        c = cands[0]
        assert c["source"] == "slack" and c["status"] == "candidate"
        assert c["scope"] == "product" and c["workspace_id"] == ws["id"]
        assert c["decided_by"] == "slack:U1"
        assert c["origin_author"] == "slack:U1"
        assert json.loads(c["links"]) == ["https://slack/p/1"]
        assert c["ref"] == "C1:100.000001"
        # watermark advanced to the max ts
        assert worker.store.slack_cursor_get("C1") == "100.000003"
        # second pass over the same messages (overlap re-scan): ref dedupe
        asyncio.run(worker._slack_ingest_once(transport=_transport(state)))
        assert len(worker.store.decision_candidates(None)) == 2

    def test_dismissed_never_recreated(self, swarm):
        worker, svc, ws = swarm
        state = {"history": {"C1": [_msg("100.000001", "!decision one-off")]}}
        asyncio.run(worker._slack_ingest_once(transport=_transport(state)))
        cand = worker.store.decision_candidates(None)[0]
        assert worker.store.decision_set_status(cand["id"], ["candidate"],
                                                "dismissed", "dashboard:m")
        asyncio.run(worker._slack_ingest_once(transport=_transport(state)))
        assert worker.store.decision_candidates(None) == []
        assert worker.store.decision_get(cand["id"])["status"] == "dismissed"

    def test_per_channel_error_isolation(self, swarm):
        worker, svc, ws = swarm
        svc.update(ws["id"], slack_channels=["CBAD", "C1"])
        worker.store.slack_cursor_set("CBAD", "1.000000")
        state = {"history": {"CBAD": "boom",
                             "C1": [_msg("100.000001", "!decision works")]}}
        asyncio.run(worker._slack_ingest_once(transport=_transport(state)))
        assert len(worker.store.decision_candidates(None)) == 1
        # the failed channel's watermark did NOT advance
        assert worker.store.slack_cursor_get("CBAD") == "1.000000"

    def test_pagination_to_exhaustion_before_advance(self, swarm):
        worker, svc, ws = swarm
        state = {"history": {"C1": {
            "": {"messages": [_msg("100.000002", "!decision late page one")],
                 "next": "cur2"},
            "cur2": {"messages": [_msg("100.000001", "!decision early page two")]},
        }}}
        asyncio.run(worker._slack_ingest_once(transport=_transport(state)))
        assert len(worker.store.decision_candidates(None)) == 2
        assert worker.store.slack_cursor_get("C1") == "100.000002"

    def test_pagination_bound_hit_holds_watermark(self, swarm):
        """History pages are NEWEST-first, so when the per-pass page bound is
        hit the unfetched remainder is the OLDER segment: the pass processes
        what it fetched (candidates still flow) but the watermark must HOLD —
        advancing to any fetched ts would jump past the older messages and,
        once aged out of the overlap, lose them forever."""
        worker, svc, ws = swarm
        state = {"history": {"C1": {
            "": {"messages": [_msg("200.000002", "!decision newest page")],
                 "next": "cur2"},
            "cur2": {"messages": [_msg("100.000001", "!decision buried older")]},
        }}}
        worker.settings.slack_ingest_max_pages = 1
        asyncio.run(worker._slack_ingest_once(transport=_transport(state)))
        assert [c["ref"] for c in worker.store.decision_candidates(None)] \
            == ["C1:200.000002"]
        # bound hit → watermark held, never advanced past the unfetched page
        assert worker.store.slack_cursor_get("C1") == "1.000000"
        # raising SLACK_INGEST_MAX_PAGES lets a pass reach exhaustion: the
        # older message is ingested and only then does the watermark advance
        worker.settings.slack_ingest_max_pages = 10
        asyncio.run(worker._slack_ingest_once(transport=_transport(state)))
        assert len(worker.store.decision_candidates(None)) == 2
        assert worker.store.slack_cursor_get("C1") == "200.000002"

    def test_first_allowlist_initializes_watermark_to_now(self, swarm):
        worker, svc, ws = swarm
        before = time.time() - 1
        svc.update(ws["id"], slack_channels=["C1", "CNEW"])
        cur = worker.store.slack_cursor_get("CNEW")
        assert cur is not None and float(cur) >= before
        # re-adding an existing channel never moves its watermark
        svc.update(ws["id"], slack_channels=["C1", "CNEW"])
        assert worker.store.slack_cursor_get("C1") == "1.000000"

    def test_uninitialized_cursor_starts_now_and_skips(self, swarm):
        worker, svc, ws = swarm
        with worker.store._conn() as c:
            c.execute("DELETE FROM slack_cursors WHERE channel = 'C1'")
        state = {"history": {"C1": [_msg("100.000001", "!decision historical")]}}
        asyncio.run(worker._slack_ingest_once(transport=_transport(state)))
        # no candidate from history; watermark now initialized
        assert worker.store.decision_candidates(None) == []
        assert worker.store.slack_cursor_get("C1") is not None


class TestWorkspaceChannels:
    def test_validation_and_cross_workspace_claim(self, store, settings):
        from app.workspaces import WorkspaceError, WorkspaceService

        svc = WorkspaceService(store, settings)
        svc.ensure_default()
        ws = store.workspace_get_by_slug("default")
        svc.update(ws["id"], slack_channels=["C1", "C2"])
        assert svc.slack_channels_of(store.workspace_get(ws["id"])) == ["C1", "C2"]
        with pytest.raises(WorkspaceError):
            svc.update(ws["id"], slack_channels="not json [")
        with pytest.raises(WorkspaceError):
            svc.update(ws["id"], slack_channels=[""])
        with pytest.raises(WorkspaceError):
            svc.update(ws["id"], slack_channels=["C%d" % i for i in range(60)])
        # a channel routes to exactly one workspace
        with pytest.raises(WorkspaceError):
            svc.create("other", "Other", slack_channels=["C1"])
        # '' clears
        svc.update(ws["id"], slack_channels="")
        assert svc.slack_channels_of(store.workspace_get(ws["id"])) == []
        ws2 = svc.create("other", "Other", slack_channels=["C1"])
        assert svc.slack_channels_of(ws2) == ["C1"]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-super-secret")

    from app import config

    config.get_settings.cache_clear()
    import importlib

    from app import main as main_module
    from fastapi.testclient import TestClient

    importlib.reload(main_module)
    with TestClient(main_module.app) as c:
        yield c
    config.get_settings.cache_clear()


class TestCandidateApi:
    def _member(self, client, name="meg"):
        store = client.app.state.store
        client.post("/api/users", headers=AUTH,
                    json={"username": name, "password": "longenough",
                          "role": "member"})
        store.user_set(name, must_change_pw=0)
        return {"Authorization": "Basic "
                + base64.b64encode(f"{name}:longenough".encode()).decode()}

    def _candidate(self, store, ws_id, text="!decision adopt kafka", ref="C1:1"):
        return store.decision_add(
            "slack", text, ref=ref, status="candidate", scope="product",
            workspace_id=ws_id, title="adopt kafka",
            decided_by="slack:U1", origin_author="slack:U1")

    def test_inbox_candidates_membership_scoped_and_badge_unchanged(self, client):
        store = client.app.state.store
        ws = store.workspace_get_by_slug("default")
        self._candidate(store, ws["id"])
        meg = self._member(client)
        r = client.get("/api/inbox", headers=meg)
        assert r.json()["candidates"] == []
        assert r.json()["counts"]["candidates"] == 0
        store.workspace_member_set(ws["id"], store.user_get("meg")["id"], True)
        r = client.get("/api/inbox", headers=meg)
        data = r.json()
        assert len(data["candidates"]) == 1
        assert data["candidates"][0]["origin_author"] == "slack:U1"
        # amendment 18: badge semantics unchanged — mine excludes candidates
        assert data["counts"]["mine"] == 0
        assert data["counts"]["candidates"] == 1
        assert data["items"] == []
        # admin sees all
        r = client.get("/api/inbox", headers=AUTH)
        assert len(r.json()["candidates"]) == 1

    def test_confirm_edits_validates_and_lands_in_fts(self, client):
        store = client.app.state.store
        ws = store.workspace_get_by_slug("default")
        did = self._candidate(store, ws["id"])
        # confirm runs the same validation as create (amendment 3)
        r = client.post(f"/api/decisions/{did}/confirm", headers=AUTH,
                        json={"scope": "galaxy"})
        assert r.status_code == 400
        r = client.post(f"/api/decisions/{did}/confirm", headers=AUTH,
                        json={"text": "   "})
        assert r.status_code == 400
        r = client.post(f"/api/decisions/{did}/confirm", headers=AUTH,
                        json={"scope": "repo", "title": "Kafka",
                              "text": "we adopt kafka for queues"})
        assert r.status_code == 200
        row = store.decision_get(did)
        assert row["status"] == "active" and row["scope"] == "repo"
        assert row["decided_by"] == "dashboard:gumo"      # the ratifying human
        assert row["origin_author"] == "slack:U1"         # amendment 2: preserved
        if store.fts_enabled:
            hits = store.fts_search(["kafka"], project="", workspace_id=ws["id"])
            assert len(hits) == 1
        # a second confirm loses the CAS
        r = client.post(f"/api/decisions/{did}/confirm", headers=AUTH, json={})
        assert r.status_code == 409

    def test_member_cannot_confirm_org_scope(self, client):
        store = client.app.state.store
        ws = store.workspace_get_by_slug("default")
        did = self._candidate(store, ws["id"])
        meg = self._member(client)
        store.workspace_member_set(ws["id"], store.user_get("meg")["id"], True)
        r = client.post(f"/api/decisions/{did}/confirm", headers=meg,
                        json={"scope": "org"})
        assert r.status_code == 403
        r = client.post(f"/api/decisions/{did}/confirm", headers=meg, json={})
        assert r.status_code == 200

    def test_dismiss_cas_and_membership(self, client):
        store = client.app.state.store
        ws = store.workspace_get_by_slug("default")
        did = self._candidate(store, ws["id"])
        meg = self._member(client)  # not a member -> 404, no leak
        r = client.post(f"/api/decisions/{did}/dismiss", headers=meg)
        assert r.status_code == 404
        r = client.post(f"/api/decisions/{did}/dismiss", headers=AUTH)
        assert r.status_code == 200
        assert store.decision_get(did)["status"] == "dismissed"
        r = client.post(f"/api/decisions/{did}/dismiss", headers=AUTH)
        assert r.status_code == 409

    def test_token_never_in_any_api_response(self, client):
        for path in ("/api/workspaces", "/api/context", "/api/me"):
            r = client.get(path, headers=AUTH)
            assert "xoxb-super-secret" not in r.text
        # workspace public shape carries channels but no token key
        r = client.get("/api/workspaces", headers=AUTH)
        w = r.json()[0]
        assert "slack_channels" in w
        assert "slack_bot_token" not in json.dumps(w)

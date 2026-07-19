"""Integration-review regressions: defects that only appear when epics COEXIST.

Each test below pins a fix for a cross-epic interaction no single-epic review
could see (Epic E3 RBAC v2 vs Epic D/I write endpoints; Epic F2 multi-worker
claim/queue vs the pre-existing enqueue + release + watch-spawn paths).
"""

import base64

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import Settings
from app.db import JobStore
from app.worker import Worker


AUTH = {"Authorization": "Basic " + base64.b64encode(b"gumo:test").decode()}


def _basic(user, pw):
    return {"Authorization": "Basic "
            + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test")
    from app import config
    config.get_settings.cache_clear()
    import importlib
    from app import main as main_module
    importlib.reload(main_module)
    with TestClient(main_module.app) as c:
        yield c
    config.get_settings.cache_clear()


def _viewer(client, name="vic"):
    store = client.app.state.store
    client.post("/api/users", headers=AUTH,
                json={"username": name, "password": "longenough", "role": "member"})
    store.user_set(name, must_change_pw=0)
    ws = store.workspace_get_by_slug("default")
    uid = store.user_get(name)["id"]
    store.workspace_member_set(ws["id"], uid, True)
    store.workspace_member_set_role(ws["id"], uid, "viewer")
    return _basic(name, "longenough"), ws["id"]


def _member(client, name="mem2"):
    store = client.app.state.store
    client.post("/api/users", headers=AUTH,
                json={"username": name, "password": "longenough", "role": "member"})
    store.user_set(name, must_change_pw=0)
    ws = store.workspace_get_by_slug("default")
    store.workspace_member_set(ws["id"], store.user_get(name)["id"], True)
    return _basic(name, "longenough"), ws["id"]


# ---- Epic D2/D3 decision endpoints vs Epic E3 RBAC v2 (viewer write bypass) ----

def test_viewer_can_read_but_not_write_product_decision(client):
    viewer, _ = _viewer(client)
    # visibility still works (membership grants read)
    assert client.get("/api/decisions", headers=viewer).status_code == 200
    # …but a read-only viewer may NOT inject product memory into pipeline prompts
    r = client.post("/api/decisions", headers=viewer,
                    json={"scope": "product", "text": "viewer rule", "project": "web"})
    assert r.status_code == 403
    # a member in the same workspace still can
    member, _ = _member(client)
    r = client.post("/api/decisions", headers=member,
                    json={"scope": "product", "text": "ok", "project": "web"})
    assert r.status_code == 200


def test_viewer_cannot_confirm_or_dismiss_or_patch_decision(client):
    store = client.app.state.store
    viewer, ws_id = _viewer(client)
    # a Slack-derived candidate awaiting human ratification
    cand = store.decision_add("slack", "untrusted slack claim", scope="product",
                              workspace_id=ws_id, status="candidate")
    # confirmation is the human-ratification gate — a viewer must not satisfy it
    assert client.post(f"/api/decisions/{cand}/confirm", headers=viewer,
                       json={}).status_code == 403
    assert client.post(f"/api/decisions/{cand}/dismiss", headers=viewer).status_code == 403
    # an active product row — a viewer must not supersede it
    active = store.decision_add("manual", "active product rule", scope="product",
                                workspace_id=ws_id)
    assert client.patch(f"/api/decisions/{active}", headers=viewer,
                        json={"action": "supersede"}).status_code == 403
    # the candidate is untouched (still ratifiable by a real writer)
    assert store.decision_get(cand)["status"] == "candidate"


# ---- Epic I inbox dismiss vs Epic E3 RBAC v2 (viewer suppression) ----

def test_viewer_cannot_dismiss_inbox_notice(client):
    store = client.app.state.store
    viewer, ws_id = _viewer(client)
    store.inbox_item_add("proposal", "p-int-1", "proactive proposal",
                         workspace_id=ws_id)
    item_id = store.inbox_items_open(None)[0]["id"]
    # dismissal is an irreversible workspace-wide suppression — viewer blocked
    assert client.post(f"/api/inbox/notices/{item_id}/dismiss", headers=viewer,
                       json={}).status_code == 403
    assert store.inbox_item_get(item_id)["status"] == "open"
    # a member may dismiss (matches the sibling adopt endpoint's write-gate)
    member, _ = _member(client)
    assert client.post(f"/api/inbox/notices/{item_id}/dismiss", headers=member,
                       json={}).status_code == 200


# ---- Epic F2 release_claim worker-id guard (C2 re-queue-during-run race) ----

def test_release_claim_worker_guarded_preserves_sibling_ownership(store):
    store.insert("j-guard", source="manual", kind="task")
    store.set_status("j-guard", "running")
    store.set_fields("j-guard", claimed_by="worker-B", run_started_at=1.0)
    # worker A (whose row was re-queued mid-run and re-claimed by B) releases:
    # the guard makes it a no-op — B's ownership survives.
    store.release_claim("j-guard", "worker-A")
    assert store.get("j-guard")["claimed_by"] == "worker-B"
    # the true owner's release DOES clear it
    store.release_claim("j-guard", "worker-B")
    assert store.get("j-guard")["claimed_by"] == ""
    # unguarded (reaper path) still clears a dead worker's claim
    store.set_fields("j-guard", claimed_by="worker-dead")
    store.release_claim("j-guard")
    assert store.get("j-guard")["claimed_by"] == ""


# ---- Epic F2 _enqueue leak (unbounded queue on the DB-authoritative path) ----

def test_enqueue_is_noop_on_multi_worker(tmp_path):
    pg_settings = Settings(data_dir=str(tmp_path), dashboard_password="test",
                           database_url="postgresql://x/y")
    assert pg_settings.multi_worker is True
    store = JobStore(str(tmp_path / "mw.db"))
    worker = Worker(pg_settings, store)
    for i in range(50):
        worker._enqueue(f"j{i}", 1)
    # nothing accumulates in the never-drained in-process queue
    assert worker.queue.qsize() == 0


def test_enqueue_still_feeds_queue_on_single_worker(worker):
    assert worker.settings.multi_worker is False
    worker._enqueue("j-sw", 1)
    assert worker.queue.qsize() == 1

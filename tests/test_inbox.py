"""Epic A4: GET /api/inbox — the per-person work queue. Items = gates the
authed user owns + unassigned gates (no enforceable DRI), membership-scoped,
sorted overdue first then oldest gate first."""

import base64
import time

import pytest
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Basic " + base64.b64encode(b"gumo:test").decode()}


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


def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def _gate(store, job_id, stage=5, hours_ago=1.0, **fields):
    store.feature_intake(job_id, title=job_id, project="demo", stage=stage)
    if fields:
        store.set_fields(job_id, **fields)
    store.set_status(job_id, "awaiting_input")
    rid = store.stage_run_open(job_id, stage, 1)
    with store._conn() as c:
        c.execute("UPDATE stage_runs SET gate_posted_at = ? WHERE id = ?",
                  (time.time() - hours_ago * 3600, rid))
    return rid


def test_inbox_requires_auth(client):
    assert client.get("/api/inbox").status_code == 401


def test_inbox_items_counts_and_ordering(client):
    store = client.app.state.store
    ws_id = client.get("/api/workspaces", headers=AUTH).json()[0]["id"]
    # iris owns feat-i2 (username DRI); feat-i1 is unassigned; feat-i3 belongs
    # to someone else entirely (never in iris's or the admin's queue)
    client.post("/api/users", headers=AUTH, json={"username": "iris", "password": "password1"})
    client.put(f"/api/workspaces/{ws_id}/members", headers=AUTH,
               json={"username": "iris", "member": True})
    _gate(store, "feat-i1", stage=3, hours_ago=1, workspace_id=ws_id)      # unassigned, fresh
    _gate(store, "feat-i2", stage=5, hours_ago=30, workspace_id=ws_id,     # iris, overdue
          dev_dri="iris")
    _gate(store, "feat-i3", stage=5, hours_ago=50, workspace_id=ws_id,     # other's, overdue
          dev_dri="somebody-else")

    iris = _basic("iris", "password1")
    data = client.get("/api/inbox", headers=iris).json()
    ids = [i["issue_id"] for i in data["items"]]
    assert ids == ["feat-i2", "feat-i1"]  # overdue first, other's gate excluded
    assert data["counts"] == {"mine": 2, "unassigned": 1, "overdue": 1}
    mine = data["items"][0]
    assert mine["gate_owner"]["is_you"] is True
    assert mine["overdue"] is True and mine["sla_hours"] == 24
    assert data["items"][1]["unassigned"] is True and data["items"][1]["gate_owner"] is None

    # the admin sees the unassigned gate but NOT other people's owned gates
    data = client.get("/api/inbox", headers=AUTH).json()
    ids = [i["issue_id"] for i in data["items"]]
    assert "feat-i1" in ids and "feat-i2" not in ids and "feat-i3" not in ids


def test_inbox_ordering_oldest_first_within_class(client):
    store = client.app.state.store
    ws_id = client.get("/api/workspaces", headers=AUTH).json()[0]["id"]
    _gate(store, "feat-o1", hours_ago=30, workspace_id=ws_id)
    _gate(store, "feat-o2", hours_ago=60, workspace_id=ws_id)
    _gate(store, "feat-o3", hours_ago=2, workspace_id=ws_id)
    _gate(store, "feat-o4", hours_ago=5, workspace_id=ws_id)
    ids = [i["issue_id"] for i in client.get("/api/inbox", headers=AUTH).json()["items"]]
    assert ids == ["feat-o2", "feat-o1", "feat-o4", "feat-o3"]


def test_inbox_membership_scoped_no_leak(client):
    store = client.app.state.store
    ws_id = client.get("/api/workspaces", headers=AUTH).json()[0]["id"]
    _gate(store, "feat-m1", workspace_id=ws_id)
    client.post("/api/users", headers=AUTH, json={"username": "outsider",
                                                  "password": "password1"})
    data = client.get("/api/inbox", headers=_basic("outsider", "password1")).json()
    assert data["items"] == []
    assert data["counts"] == {"mine": 0, "unassigned": 0, "overdue": 0}


def test_inbox_includes_v1_awaiting_and_feature_errors(client):
    store = client.app.state.store
    ws_id = client.get("/api/workspaces", headers=AUTH).json()[0]["id"]
    # a v1 request awaiting input: unassigned, overdue flag rides updated_at
    store.insert("task-i1", source="manual", kind="task", project="demo", title="t")
    store.set_fields("task-i1", workspace_id=ws_id)
    store.set_status("task-i1", "awaiting_input")
    # a feature in error is answerable (redo) and shows up
    store.feature_intake("feat-e1", title="e", project="demo", stage=4)
    store.set_fields("feat-e1", workspace_id=ws_id)
    store.set_status("feat-e1", "error")
    # a v1 error is NOT answerable and must not appear
    store.insert("task-i2", source="manual", kind="task", project="demo", title="t2")
    store.set_fields("task-i2", workspace_id=ws_id)
    store.set_status("task-i2", "error")

    ids = {i["issue_id"] for i in client.get("/api/inbox", headers=AUTH).json()["items"]}
    assert ids == {"task-i1", "feat-e1"}


def test_inbox_workspace_sla_override(client):
    store = client.app.state.store
    ws_id = client.get("/api/workspaces", headers=AUTH).json()[0]["id"]
    client.patch(f"/api/workspaces/{ws_id}", headers=AUTH, json={"gate_sla_hours": 1})
    _gate(store, "feat-w1", hours_ago=2, workspace_id=ws_id)  # 2h > 1h workspace SLA
    item = client.get("/api/inbox", headers=AUTH).json()["items"][0]
    assert item["sla_hours"] == 1 and item["overdue"] is True

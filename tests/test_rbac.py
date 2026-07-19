"""Epic E3 — RBAC v2: role migration, scoped roles, per-repo restriction."""

import base64
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import rbac
from app.db import JobStore, SCHEMA, normalize_role


AUTH = {"Authorization": "Basic " + base64.b64encode(b"gumo:test").decode()}


def _basic(u, p):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


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
        c._store = main_module.app.state.store
        yield c
    config.get_settings.cache_clear()


# --- migration + normalization ---

def test_legacy_admin_migrated_and_normalized(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO users (username, pw_hash, role, created_at, updated_at) "
                 "VALUES ('old', 'h', 'admin', 0, 0)")
    conn.commit()
    conn.close()
    store = JobStore(path)  # one-shot UPDATE runs
    assert store.user_get("old")["role"] == "instance_admin"


def test_read_side_normalization_shim():
    assert normalize_role("admin") == "instance_admin"
    assert normalize_role("member") == "member"
    assert normalize_role("viewer") == "viewer"


def test_bootstrap_admin_is_instance_admin(client):
    assert client._store.user_get("gumo")["role"] == "instance_admin"


# --- rbac module logic ---

def test_workspace_role_resolution(store):
    admin = store.user_create("adm", "h", role="instance_admin")
    member = store.user_create("mem", "h", role="member")
    ws = store.workspace_create("W", "w") if hasattr(store, "workspace_create") else None
    wid = ws["id"] if ws else 1
    # instance admin is admin everywhere
    assert rbac.workspace_role(store, admin, wid) == "admin"
    # non-member -> None
    assert rbac.workspace_role(store, member, wid) is None
    store.workspace_member_set(wid, member["id"], True)
    store.workspace_member_set_role(wid, member["id"], "viewer")
    assert rbac.workspace_role(store, member, wid) == "viewer"
    assert rbac.is_read_only("viewer") is True
    assert rbac.is_read_only("member") is False


def test_per_repo_restriction(store):
    member = store.user_create("m2", "h", role="member")
    ws = store.workspace_create("W2", "w2") if hasattr(store, "workspace_create") else None
    wid = ws["id"] if ws else 1
    store.workspace_member_set(wid, member["id"], True)
    store.workspace_member_set_role(wid, member["id"], "member", ["web"])
    assert rbac.can_submit(store, member, wid, "web") is True
    assert rbac.can_submit(store, member, wid, "demo") is False  # not in allow-list
    # empty repos = all
    store.workspace_member_set_role(wid, member["id"], "member", [])
    assert rbac.can_submit(store, member, wid, "demo") is True
    # viewer can never submit
    store.workspace_member_set_role(wid, member["id"], "viewer", [])
    assert rbac.can_submit(store, member, wid, "web") is False


def test_can_configure_workspace(store):
    admin = store.user_create("a3", "h", role="instance_admin")
    wsadmin = store.user_create("wa", "h", role="member")
    ws = store.workspace_create("W3", "w3") if hasattr(store, "workspace_create") else None
    wid = ws["id"] if ws else 1
    store.workspace_member_set(wid, wsadmin["id"], True)
    store.workspace_member_set_role(wid, wsadmin["id"], "admin")
    assert rbac.can_configure_workspace(store, admin, wid) is True
    assert rbac.can_configure_workspace(store, wsadmin, wid) is True
    other = store.user_create("o", "h", role="member")
    assert rbac.can_configure_workspace(store, other, wid) is False


# --- endpoint behaviour ---

def test_member_cannot_configure_instance(client):
    client.post("/api/users", headers=AUTH,
                json={"username": "dev", "password": "devpass12", "role": "member"})
    m = _basic("dev", "devpass12")
    assert client.put("/api/context", headers=m, json={"product_name": "X"}).status_code == 403
    assert client.get("/api/users", headers=m).status_code == 403
    # viewer GET still works (read-only)
    assert client.get("/api/jobs", headers=m).status_code == 200


def test_role_validation_rejects_bad_role(client):
    r = client.post("/api/users", headers=AUTH,
                    json={"username": "x", "password": "passpass1", "role": "superuser"})
    assert r.status_code == 400
    # legacy 'admin' is accepted as an alias -> instance_admin
    r2 = client.post("/api/users", headers=AUTH,
                     json={"username": "leg", "password": "passpass1", "role": "admin"})
    assert r2.status_code == 200 and r2.json()["role"] == "instance_admin"


def test_cannot_demote_last_admin(client):
    # gumo is the only instance admin
    r = client.patch("/api/users/gumo", headers=AUTH, json={"role": "member"})
    assert r.status_code == 400

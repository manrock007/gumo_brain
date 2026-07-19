"""Regression tests for the Phase 3 (Epic E/G) adversarial-review fixes:
RBAC v2 endpoint enforcement, user-mutation audit, token-scope rejection, and
budget enforcement decoupled from the intake `forced` flag."""

import asyncio
import base64
import time

import pytest
from fastapi.testclient import TestClient


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


def _default_ws_id(client):
    return client.get("/api/workspaces", headers=AUTH).json()[0]["id"]


# ---- RBAC v2: viewer read-only + per-repo restriction ARE enforced on writes ----

def test_viewer_cannot_submit_feature_or_task(client):
    client.post("/api/users", headers=AUTH,
                json={"username": "vic", "password": "viewerpw1", "role": "member"})
    wid = _default_ws_id(client)
    vic = client._store.user_get("vic")
    client._store.workspace_member_set(wid, vic["id"], True)
    client._store.workspace_member_set_role(wid, vic["id"], "viewer")
    m = _basic("vic", "viewerpw1")
    # a read-only viewer is rejected on every write path (before any side effect)
    assert client.post("/api/features", headers=m,
                       json={"project": "web", "title": "x", "request": "y"}).status_code == 403
    assert client.post("/api/tasks", headers=m,
                       json={"project": "web", "title": "x", "request": "y"}).status_code == 403


def test_repo_restricted_member_blocked_off_allowlist(client):
    client.post("/api/users", headers=AUTH,
                json={"username": "car", "password": "carolpw12", "role": "member"})
    wid = _default_ws_id(client)
    car = client._store.user_get("car")
    client._store.workspace_member_set(wid, car["id"], True)
    client._store.workspace_member_set_role(wid, car["id"], "member", ["web"])
    m = _basic("car", "carolpw12")
    # 'demo' is not in the allow-list -> 403 (per-repo restriction enforced)
    assert client.post("/api/features", headers=m,
                       json={"project": "demo", "title": "x", "request": "y"}).status_code == 403


# ---- Epic E4: the security-sensitive user mutations are audited ----

def test_user_mutations_audited(client):
    store = client._store
    client.post("/api/users", headers=AUTH,
                json={"username": "mal", "password": "mallory12", "role": "member"})
    client.patch("/api/users/mal", headers=AUTH, json={"role": "instance_admin"})
    client.patch("/api/users/mal", headers=AUTH, json={"password": "newpass123"})
    client.patch("/api/users/mal", headers=AUTH, json={"disabled": True})
    actions = [r["action"] for r in store.audit_recent()]
    assert "user.create" in actions
    assert "user.role" in actions      # privilege grant
    assert "user.disable" in actions   # offboarding
    assert "user.update" in actions    # forced password reset


# ---- Epic E2: an unenforced scoped token is rejected, not silently full-power ----

def test_scoped_token_rejected(client):
    r = client.post("/api/tokens", headers=AUTH, json={"name": "ro", "scopes": ["read"]})
    assert r.status_code == 400
    # an empty/omitted scopes list still mints a (full-role) token
    assert client.post("/api/tokens", headers=AUTH, json={"name": "full"}).status_code == 200


# ---- Epic G4: a forced feature job is STILL budget-blocked (forced != override) ----

def test_forced_feature_job_is_still_budget_blocked(worker):
    """The core Epic G4 regression: intake stamps forced=1 on every feature job,
    but that must NOT exempt it from the budget block. Only an explicit
    budget_override (admin re-kick) proceeds — and it is consumed one-shot."""
    store = worker.store
    settings = worker.settings
    settings.budget_monthly_usd = 10
    settings.budget_block_enabled = True
    ws = store.workspace_create("Web", "web")
    # a feature job born forced=1 (exactly as db.feature_intake stamps it)
    store.insert("feat-forced", source="manual", forced=True, kind="feature",
                 project="web", title="x")
    store.set_fields("feat-forced", status="queued", stage=3, workspace_id=ws["id"])
    store.stage_run_open("feat-forced", 1, 1, time.time())
    run = store.stage_runs_for("feat-forced")[0]
    with store._conn() as c:
        c.execute("UPDATE stage_runs SET cost_usd = 999, started_at = ? WHERE id = ?",
                  (time.time(), run["id"]))
    asyncio.run(worker.engine.run_stage(store.get("feat-forced")))
    after = store.get("feat-forced")
    assert after["status"] == "error"          # forced no longer exempts the block
    assert after["stage"] == 3                 # pipeline state preserved
    assert any(r["action"] == "budget.block" for r in store.audit_recent())


def test_budget_override_is_one_shot(worker):
    """An explicit budget_override lets the block pass AND is consumed (cleared)
    by the engine before the run, so the NEXT over-budget stage re-parks."""
    store = worker.store
    settings = worker.settings
    settings.budget_monthly_usd = 10
    settings.budget_block_enabled = True
    ws = store.workspace_create("Web", "web")
    store.insert("feat-ov", source="manual", forced=True, kind="feature",
                 project="web", title="x")
    store.set_fields("feat-ov", status="queued", stage=2, workspace_id=ws["id"],
                     budget_override=1)
    store.stage_run_open("feat-ov", 1, 1, time.time())
    run = store.stage_runs_for("feat-ov")[0]
    with store._conn() as c:
        c.execute("UPDATE stage_runs SET cost_usd = 999, started_at = ? WHERE id = ?",
                  (time.time(), run["id"]))

    # stop the stage right after the budget gate so we don't drive a real run —
    # by then the override must already be consumed and the block passed.
    class _Stop(Exception):
        pass

    def _boom(job):
        raise _Stop()

    worker.engine._branch = _boom
    with pytest.raises(_Stop):
        asyncio.run(worker.engine.run_stage(store.get("feat-ov")))
    after = store.get("feat-ov")
    assert after["status"] != "error"          # override let the stage past the block
    assert after["budget_override"] == 0       # one-shot consumed
    assert any(r["action"] == "budget.override" for r in store.audit_recent())

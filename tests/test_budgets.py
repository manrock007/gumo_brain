"""Epic G4 — budgets & spend enforcement."""

import asyncio
import time

import pytest

from app import budgets
from app.config import Settings


def _ws(bid=None):
    return {"id": 1, "slug": "web", "budget_monthly_usd": bid}


class _FakeStore:
    def __init__(self, spent):
        self._spent = spent
        self.added = []

    def costs_since(self, since):
        return {1: self._spent}

    def workspace_get(self, wid):
        return _ws(self._budget)

    def set_status(self, *a, **k):
        self.added.append(("status", a, k))

    def inbox_item_add(self, *a, **k):
        self.added.append(("inbox", a, k))

    def audit_add(self, *a, **k):
        self.added.append(("audit", a, k))


def test_budget_status_thresholds():
    s = Settings(budget_warn_pct=80)
    store = _FakeStore(spent=0)
    # ok
    assert budgets.budget_status(store, s, _ws(100))["state"] == "ok"
    # warn
    store._spent = 85
    assert budgets.budget_status(store, s, _ws(100))["state"] == "warn"
    # block
    store._spent = 120
    st = budgets.budget_status(store, s, _ws(100))
    assert st["state"] == "block" and st["pct"] == 120.0


def test_budget_zero_is_inert():
    s = Settings()
    store = _FakeStore(spent=9999)
    st = budgets.budget_status(store, s, _ws(0))
    assert st["state"] == "ok" and st["budget"] == 0.0


def test_instance_fallback_budget():
    s = Settings(budget_monthly_usd=50)
    store = _FakeStore(spent=60)
    # ws budget NULL -> inherit instance 50 -> over -> block
    st = budgets.budget_status(store, s, _ws(None))
    assert st["state"] == "block"


def test_should_block_respects_override_and_flag():
    s = Settings(budget_block_enabled=True)
    store = _FakeStore(spent=200)
    blocked, _ = budgets.should_block(store, s, _ws(100), override=False)
    assert blocked is True
    # an explicit budget override never blocks
    assert budgets.should_block(store, s, _ws(100), override=True)[0] is False
    # block disabled -> warn-only, never blocks
    s2 = Settings(budget_block_enabled=False)
    assert budgets.should_block(store, s2, _ws(100), override=False)[0] is False


def test_feature_stage_budget_block_preserves_state(worker):
    """A non-forced feature stage at >=100% parks as error (state preserved),
    audits BUDGET_BLOCK, and never opens a run."""
    store = worker.store
    settings = worker.settings
    settings.budget_monthly_usd = 10
    settings.budget_block_enabled = True
    ws = store.workspace_create("Web", "web") if hasattr(store, "workspace_create") else None
    # build a feature job with a workspace and heavy prior spend
    store.insert("feat-b1", source="dashboard", kind="feature", project="web", title="x")
    wsid = ws["id"] if ws else None
    store.set_fields("feat-b1", status="queued", stage=2, workspace_id=wsid)
    # plant spend via a stage_run row over budget
    store.stage_run_open("feat-b1", 1, 1, time.time())
    runs = store.stage_runs_for("feat-b1")
    with store._conn() as c:
        c.execute("UPDATE stage_runs SET cost_usd = 999, started_at = ? WHERE id = ?",
                  (time.time(), runs[0]["id"]))
    job = store.get("feat-b1")
    asyncio.run(worker.engine.run_stage(job))
    after = store.get("feat-b1")
    assert after["status"] == "error"
    assert after["stage"] == 2  # stage preserved
    assert any(r["action"] == "budget.block" for r in store.audit_recent())


def test_budgets_api(client_and_store):
    client, store = client_and_store
    r = client.get("/api/budgets")
    assert r.status_code == 200
    assert "budgets" in r.json()


@pytest.fixture()
def client_and_store(tmp_path, monkeypatch):
    import base64
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test")
    from app import config
    config.get_settings.cache_clear()
    import importlib
    from app import main as main_module
    importlib.reload(main_module)
    auth = {"Authorization": "Basic " + base64.b64encode(b"gumo:test").decode()}
    with TestClient(main_module.app) as c:
        c.headers.update(auth)
        yield c, main_module.app.state.store
    config.get_settings.cache_clear()

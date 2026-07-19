"""Epic 0.4 — install-from-zero proof: a fresh instance boots with NO Gumo
assumption anywhere. Empty config → first-run wizard → add a workspace/repo →
submit a feature — green without touching any Gumo value.

The suite-wide conftest pins a demo REPO_MAP env for the other tests; these
tests explicitly clear it (and every other mapped var) to reach the true
neutral state."""

import base64
import importlib
import json

import pytest

# every env var whose Settings field carried a customer default before Epic 0
NEUTRALIZED_ENV = [
    "REPO_MAP", "PRODUCT_NAME", "BUSINESS_CONTEXT", "MEMORY_CANONICAL_PROJECT",
    "SENTRY_ORG", "SENTRY_API_BASE", "SENTRY_WEB_BASE", "SENTRY_AUTH_TOKEN",
    "CLICKUP_TOKEN", "CLICKUP_LIST_ID", "CLICKUP_STAGE_FIELD_MAP",
    "CLICKUP_REPO_STAGE_MAP", "CLICKUP_PR_FIELD_MAP", "CLICKUP_DOC_FIELD_MAP",
    "CLICKUP_FOLDER_FIELD", "CLICKUP_FRICTION_FIELD", "CLICKUP_FLAG_FIELD",
    "CLICKUP_METRIC_FIELD", "PUBLIC_BASE_URL", "GITHUB_TOKEN", "BRANCH_PREFIX",
    "DASHBOARD_PASSWORD",
]


def _clean_env(monkeypatch):
    for var in NEUTRALIZED_ENV:
        monkeypatch.delenv(var, raising=False)


def test_neutral_defaults(monkeypatch):
    """Settings() under a clean env is fully neutral — and its serialized form
    carries no customer string anywhere."""
    _clean_env(monkeypatch)
    from app.config import Settings

    s = Settings()
    assert json.loads(s.repo_map) == {}
    assert s.product_name == "your product"
    assert s.business_context == ""
    assert s.memory_canonical_project == ""
    assert s.sentry_org == "" and not s.sentry_enabled
    assert s.sentry_api_base == "https://sentry.io/api/0"
    assert s.clickup_list_id == "" and not s.clickup_enabled
    assert json.loads(s.clickup_stage_field_map) == {}
    assert json.loads(s.clickup_repo_stage_map) == {}
    assert json.loads(s.clickup_pr_field_map) == {}
    assert json.loads(s.clickup_doc_field_map) == {}
    assert s.clickup_folder_field == "" and s.clickup_friction_field == ""
    assert s.clickup_flag_field == "" and s.clickup_metric_field == ""
    assert s.public_base_url == ""
    assert s.branch_prefix == "ctrlloop"
    dumped = json.dumps(s.model_dump()).lower()
    assert "gumo" not in dumped
    assert "manrock007" not in dumped


def test_no_customer_strings_in_prompts():
    """The real prompt builders, fed the fresh-instance context, emit nothing
    customer-specific."""
    from app.config import RepoTarget
    from app.feature_prompts import build_bootstrap_prompt, build_stage_prompt
    from app.prompts import build_fix_prompt, build_task_plan_prompt

    target = RepoTarget(repo="acme/demo", base="main")
    job = {"issue_id": "feat-1", "title": "first feature", "project": "demo",
           "request": "Build the first thing."}
    stage_prompts = [build_stage_prompt(
        target=target, branch="ctrlloop/feat-feat-1", job=job, stage=stage,
        memory_context="", artifact_names=["P0-intake.md"], inline_artifacts={},
        guidance_entries=[]) for stage in range(10)]
    issue = {"title": "boom", "url": "u", "id": "1", "project": "demo",
             "culprit": "c", "times_seen": 1, "users_affected": 1}
    fix_prompt = build_fix_prompt(target=target, branch="ctrlloop/sentry-1",
                                  issue=issue, stacktrace="tb", clickup_task_id=None)
    task_prompt = build_task_plan_prompt(
        target=target, branch="ctrlloop/task-1",
        task={"title": "t", "url": "", "id": "task-1", "project": "demo"},
        request="r", clickup_task_id=None)
    boot_prompt = build_bootstrap_prompt(target=target, branch="b", project="demo",
                                         is_canonical=True, run=2)
    for prompt in (*stage_prompts, fix_prompt, task_prompt, boot_prompt):
        assert "gumo" not in prompt.lower()
        assert "manrock007" not in prompt.lower()


@pytest.fixture()
def zero_client(tmp_path, monkeypatch):
    """An app booted from the true from-zero state: clean env, empty data dir,
    only an admin password. The worker's job processor is stubbed so no clone
    or network is ever attempted."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CTRLLOOP_ADMIN_PASSWORD", "first-boot-pass")

    from fastapi.testclient import TestClient

    from app import config

    config.get_settings.cache_clear()
    import app.worker as worker_mod
    from app import main as main_module

    importlib.reload(main_module)

    async def _noop(self, job, queued_at=None):
        pass

    monkeypatch.setattr(worker_mod.Worker, "_process", _noop)
    with TestClient(main_module.app) as c:
        yield c
    config.get_settings.cache_clear()


ADMIN = {"Authorization": "Basic " + base64.b64encode(b"admin:first-boot-pass").decode()}


def test_fresh_boot_to_feature(zero_client):
    c = zero_client
    # first-run wizard: needed, every step untouched
    setup = c.get("/api/setup", headers=ADMIN).json()
    assert setup["needed"] is True
    assert setup["steps"] == {"business_context": False, "repos": False,
                              "github_token": False, "memory": False, "team": False}

    # the migration wrapped the neutral context into one empty workspace
    ws_list = c.get("/api/workspaces", headers=ADMIN).json()
    assert len(ws_list) == 1
    ws = ws_list[0]
    assert ws["slug"] == "default" and ws["name"] == "Default"
    assert ws["repos"] == {}
    assert ws["product_name"] == ""  # falls through to the instance value
    assert c.get("/api/projects", headers=ADMIN).json() == []

    # the wizard's first real step: point the workspace at a repo
    r = c.patch(f"/api/workspaces/{ws['id']}", headers=ADMIN,
                json={"repos": [{"slug": "demo", "repo": "acme/demo", "base": "main"}],
                      "canonical_project": "demo"})
    assert r.status_code == 200
    assert r.json()["repos"]["demo"]["repo"] == "acme/demo"
    projects = c.get("/api/projects", headers=ADMIN).json()
    assert [p["slug"] for p in projects] == ["demo"]

    # submit the first feature — no ClickUp configured, so no ticket, still queued
    r = c.post("/api/features", headers=ADMIN,
               json={"project": "demo", "title": "first"})
    assert r.status_code == 200
    body = r.json()
    assert "queued" in body["decision"]
    assert body["clickup_task_url"] is None
    job = c.app.state.store.get(body["job_id"])
    assert job["kind"] == "feature" and job["stage"] == 0

    # the wizard tracked the progress
    steps = c.get("/api/setup", headers=ADMIN).json()["steps"]
    assert steps["repos"] is True

    # Sentry lane is cleanly off, not broken
    r = c.post("/api/trigger", headers=ADMIN, json={"issue": "12345"})
    assert r.status_code == 400 and "not configured" in r.json()["detail"]


def test_empty_map_edges(zero_client):
    c = zero_client
    # setup_status with the EMPTY default map must not raise (allow_empty)
    assert c.get("/api/setup", headers=ADMIN).status_code == 200

    # product_name-only context edit works with canonical ""
    r = c.put("/api/context", headers=ADMIN, json={"product_name": "Acme"})
    assert r.status_code == 200
    assert r.json()["context"]["product_name"] == "Acme"
    assert c.get("/api/setup", headers=ADMIN).json()["steps"]["business_context"] is True
    # the rendered dashboard follows it
    assert "the Acme Engine" in c.get("/", headers=ADMIN).text

    # DELETE reverts to the neutral defaults
    r = c.delete("/api/context", headers=ADMIN)
    assert r.status_code == 200
    ctx = r.json()["context"]
    assert ctx["product_name"] == "your product"
    assert ctx["business_context"] == ""
    assert ctx["canonical_project"] == ""

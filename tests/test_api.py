import base64

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


def test_health_open(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_auth_required(client):
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/projects").status_code == 401


def test_projects_lists_repo_map(client):
    r = client.get("/api/projects", headers=AUTH)
    assert r.status_code == 200
    slugs = {p["slug"] for p in r.json()}
    assert "web" in slugs and "gumo" in slugs


def test_task_validation(client):
    r = client.post("/api/tasks", headers=AUTH, json={"project": "nope", "title": "x"})
    assert r.status_code == 400
    r = client.post("/api/tasks", headers=AUTH, json={"project": "web"})
    assert r.status_code == 400
    r = client.post("/api/tasks", headers=AUTH, json={"project": "web", "clickup": "not a url !!"})
    assert r.status_code == 400
    # valid ClickUp ref but integration disabled -> 404
    r = client.post(
        "/api/tasks", headers=AUTH,
        json={"project": "web", "clickup": "https://app.clickup.com/t/86czkq999"},
    )
    assert r.status_code == 404


def test_answer_validation(client):
    r = client.post("/api/jobs/none/answer", headers=AUTH, json={"action": "bogus"})
    assert r.status_code == 400
    r = client.post("/api/jobs/none/answer", headers=AUTH, json={"action": "proceed"})
    assert r.status_code == 404
    r = client.post("/api/jobs/none/answer", headers=AUTH, json={"action": "redo"})
    assert r.status_code == 404


def test_feature_validation(client):
    r = client.post("/api/features", headers=AUTH, json={"project": "nope", "title": "x"})
    assert r.status_code == 400
    r = client.post("/api/features", headers=AUTH, json={"project": "web"})
    assert r.status_code == 400
    r = client.post(
        "/api/features", headers=AUTH,
        json={"project": "web", "clickup": "https://app.clickup.com/t/86czkq999"},
    )
    assert r.status_code == 404  # ClickUp disabled in tests


def test_memory_endpoints(client):
    r = client.get("/api/memory", headers=AUTH)
    assert r.status_code == 200
    assert "web" in r.json()
    r = client.get("/api/memory/nope", headers=AUTH)
    assert r.status_code == 404
    r = client.get("/api/memory/web", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["exists"] is False
    r = client.post("/api/memory/nope/bootstrap", headers=AUTH)
    assert r.status_code == 404


def test_context_roundtrip(client):
    r = client.get("/api/context", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["context"]["product_name"] == "Gumo"
    assert "gumo" in data["context"]["repo_map"]
    assert data["overridden"] == []
    assert data["defaults"]["canonical_project"] == "gumo"

    # invalid payloads change nothing (atomic, fail-closed)
    r = client.put("/api/context", headers=AUTH,
                   json={"repo_map": {"x": {"repo": "not-owner-name"}}})
    assert r.status_code == 400
    r = client.put("/api/context", headers=AUTH,
                   json={"repo_map": {"x": {"repo": "o/r"}}})  # canonical 'gumo' missing
    assert r.status_code == 400
    assert client.get("/api/context", headers=AUTH).json()["overridden"] == []

    body = {
        "product_name": "Acme",
        "business_context": "Acme builds rockets.",
        "repo_map": {"api": {"repo": "acme/api", "base": "dev", "test_cmd": "pytest"},
                     "app": {"repo": "acme/app"}},
        "canonical_project": "api",
    }
    r = client.put("/api/context", headers=AUTH, json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["context"]["product_name"] == "Acme"
    assert data["context"]["canonical_project"] == "api"
    assert data["context"]["repo_map"]["app"]["base"] == "main"  # normalized default
    assert set(data["overridden"]) == {"product_name", "business_context",
                                       "repo_map", "memory_canonical_project"}
    # the rest of the app follows the new map immediately
    slugs = {p["slug"] for p in client.get("/api/projects", headers=AUTH).json()}
    assert slugs == {"api", "app"}
    assert client.post("/api/tasks", headers=AUTH,
                       json={"project": "gumo", "title": "x"}).status_code == 400

    r = client.delete("/api/context", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["context"]["product_name"] == "Gumo"
    assert r.json()["overridden"] == []
    slugs = {p["slug"] for p in client.get("/api/projects", headers=AUTH).json()}
    assert "gumo" in slugs


def test_context_requires_auth(client):
    assert client.get("/api/context").status_code == 401
    assert client.put("/api/context", json={}).status_code == 401
    assert client.delete("/api/context").status_code == 401


def test_dashboard_rebrands_from_context(client):
    """The rebrand hangs off an exact literal in DASHBOARD_HTML — pin it, and
    prove the rendered page follows the configured product name."""
    from app.dashboard import DASHBOARD_HTML

    assert "the Gumo Engine" in DASHBOARD_HTML  # main.dashboard() replaces this
    assert "the Gumo Engine" in client.get("/", headers=AUTH).text
    r = client.put("/api/context", headers=AUTH, json={"product_name": "Acme"})
    assert r.status_code == 200
    page = client.get("/", headers=AUTH).text
    assert "the Acme Engine" in page and "the Gumo Engine" not in page
    client.delete("/api/context", headers=AUTH)


def test_context_put_warns_about_unmapped_live_jobs(client):
    """Removing a slug from the map must not silently doom live jobs — the
    save response lists them (they will be skipped at next dispatch)."""
    store = client.app.state.store
    store.insert("task-live1", source="manual", title="t", project="gumo", kind="task")
    r = client.put("/api/context", headers=AUTH, json={
        "repo_map": {"api": {"repo": "acme/api"}}, "canonical_project": "api",
    })
    assert r.status_code == 200
    assert "task-live1" in r.json()["warning"]
    # partial update: only the sent fields became overrides
    assert set(r.json()["overridden"]) == {"repo_map", "memory_canonical_project"}
    # reset also warns while the job is still live and unmapped under defaults?
    # (project 'gumo' IS in the defaults, so the warning clears)
    r = client.delete("/api/context", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["warning"] == ""


def test_feature_stats_404(client):
    r = client.get("/api/features/none/stats", headers=AUTH)
    assert r.status_code == 404


def test_dashboard_serves(client):
    r = client.get("/", headers=AUTH)
    assert r.status_code == 200
    assert "Submit a request" in r.text


def test_dashboard_has_no_backslashes():
    """Load-bearing invariant: dashboard.py is one Python string that must never
    contain a backslash (JS regexes are built via new RegExp + fromCharCode), so
    Python escape handling can never mangle the emitted HTML/JS."""
    from app.dashboard import DASHBOARD_HTML

    assert "\\" not in DASHBOARD_HTML


def test_dashboard_shell_is_balanced():
    """The inbox split-view: the ids and hooks the front-end JS drives must all
    be present and the shell balanced."""
    from app.dashboard import DASHBOARD_HTML as h

    assert h.count("<script>") == h.count("</script>") == 1
    for tok in ('class="topbar"', 'id="msg"', 'id="inbox-list"', 'id="welcome"',
                'id="dpane"', 'id="d-thread"', 'id="d-title"', 'id="c-in"',
                'id="brain-body"', 'data-status="', 'STATUS_LABEL',
                'function routeHash', 'function sendComposer', 'function answer',
                'function setFilter', 'session/stream', 'chat/stream',
                # intake forms keep their ids — the submit handlers depend on them
                'id="task-project"', 'id="feat-project"', 'id="ref"',
                # the project-context editor (docs/ENGINE.md §10)
                'id="ctx-body"', 'function saveContext', 'function resetContext',
                'function loadContext'):
        assert tok in h, tok

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


def test_context_put_persist_failure_leaves_live_state(client, monkeypatch):
    """Persist-before-apply: if the DB write fails, the request errors and the
    LIVE settings are untouched — live and persisted state never diverge."""
    import sqlite3

    import pytest as _pytest

    store = client.app.state.store

    def boom(values):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "config_set_many", boom)
    with _pytest.raises(sqlite3.OperationalError):
        client.put("/api/context", headers=AUTH, json={"product_name": "Acme"})
    assert client.get("/api/context", headers=AUTH).json()["context"]["product_name"] == "Gumo"


def test_dashboard_rebrands_from_context(client):
    """The rebrand hangs off an exact literal in static/index.html — pin it,
    and prove the rendered page follows the configured product name."""
    from app.main import _INDEX_HTML

    assert "the Gumo Engine" in _INDEX_HTML  # main.dashboard() replaces this
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
    # unauthenticated browsers land on the login page, not a Basic-auth popup
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and "login" in r.headers["location"]
    assert client.get("/login").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def _static(name: str) -> str:
    from app.main import STATIC_DIR

    return (STATIC_DIR / name).read_text()


def test_dashboard_shell_is_balanced():
    """The inbox split-view: the ids and hooks the front-end JS drives must all
    be present across the static files."""
    h = _static("index.html")
    js = _static("app.js")

    for tok in ('class="topbar"', 'id="msg"', 'id="inbox-list"', 'id="welcome"',
                'id="dpane"', 'id="d-thread"', 'id="d-title"', 'id="c-in"',
                'id="brain-body"', 'id="task-project"', 'id="feat-project"', 'id="ref"',
                'id="ctx-body"',
                # auth chrome (docs/ENGINE.md §11)
                'id="me-chip"', 'id="settings-pane"', 'id="sp-users"', 'id="users-list"',
                'src="static/app.js"', 'href="static/style.css"'):
        assert tok in h, tok
    for tok in ('data-status="', 'STATUS_LABEL', 'function routeHash',
                'function sendComposer', 'function answer', 'function setFilter',
                'session/stream', 'chat/stream',
                'function saveContext', 'function resetContext', 'function loadContext',
                # auth (docs/ENGINE.md §11)
                'function loadMe', 'function signOut', 'function createUser',
                'function changePassword'):
        assert tok in js, tok


def test_login_flow_and_roles(client):
    # cookie session end to end
    r = client.post("/api/login", json={"username": "gumo", "password": "test"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert client.get("/api/me").json()["username"] == "gumo"  # cookie carried
    # wrong password is a generic 401
    bad = client.post("/api/login", json={"username": "gumo", "password": "nope"})
    assert bad.status_code == 401

    # admin creates a member; member cannot touch config or users
    r = client.post("/api/users", headers=AUTH,
                    json={"username": "dev1", "password": "devpass123"})
    assert r.status_code == 200 and r.json()["must_change_pw"] is True
    member = {"Authorization": "Basic " + base64.b64encode(b"dev1:devpass123").decode()}
    assert client.get("/api/jobs", headers=member).status_code == 200
    assert client.put("/api/context", headers=member,
                      json={"product_name": "X"}).status_code == 403
    assert client.get("/api/users", headers=member).status_code == 403

    # gate answers are attributed to the acting user
    store = client.app.state.store
    store.insert("task-attr", source="manual", title="t", project="gumo", kind="task")
    store.set_fields("task-attr", status="awaiting_input", analysis="a", question="q")
    r = client.post("/api/jobs/task-attr/answer", headers=member,
                    json={"action": "skip", "answer": ""})
    assert r.status_code == 200
    entries = store.guidance_for("task-attr")
    assert entries and entries[-1]["via"] == "dashboard:dev1"

    # disable revokes access
    assert client.patch("/api/users/dev1", headers=AUTH,
                        json={"disabled": True}).status_code == 200
    assert client.get("/api/jobs", headers=member).status_code == 401
    client.post("/api/logout")


def test_lockout_after_repeated_failures(client):
    client.post("/api/users", headers=AUTH,
                json={"username": "dev2", "password": "devpass123"})
    for _ in range(5):
        assert client.post("/api/login", json={"username": "dev2",
                                               "password": "wrong"}).status_code == 401
    r = client.post("/api/login", json={"username": "dev2", "password": "devpass123"})
    assert r.status_code == 429  # locked even with the right password


def test_change_password_revokes_sessions(client):
    client.post("/api/users", headers=AUTH,
                json={"username": "dev3", "password": "devpass123"})
    login = client.post("/api/login", json={"username": "dev3", "password": "devpass123"})
    assert login.status_code == 200
    r = client.post("/api/me/password", json={"current": "devpass123", "new": "newpass456"})
    assert r.status_code == 200
    assert client.get("/api/me").status_code == 401  # old session revoked
    fresh = {"Authorization": "Basic " + base64.b64encode(b"dev3:newpass456").decode()}
    assert client.get("/api/me", headers=fresh).json()["must_change_pw"] is False

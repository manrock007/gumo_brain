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


def test_feature_stats_404(client):
    r = client.get("/api/features/none/stats", headers=AUTH)
    assert r.status_code == 404


def test_dashboard_serves(client):
    r = client.get("/", headers=AUTH)
    assert r.status_code == 200
    assert "Submit a request" in r.text

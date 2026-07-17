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

    # repo topology moved to workspaces (Phase 2) — the context API refuses it
    # explicitly rather than accepting-and-ignoring
    r = client.put("/api/context", headers=AUTH,
                   json={"repo_map": {"x": {"repo": "o/r"}}})
    assert r.status_code == 400 and "workspace" in r.json()["detail"]
    r = client.put("/api/context", headers=AUTH, json={"canonical_project": "x"})
    assert r.status_code == 400 and "workspace" in r.json()["detail"]
    assert client.get("/api/context", headers=AUTH).json()["overridden"] == []

    r = client.put("/api/context", headers=AUTH,
                   json={"product_name": "Acme", "business_context": "Acme builds rockets."})
    assert r.status_code == 200
    data = r.json()
    assert data["context"]["product_name"] == "Acme"
    assert set(data["overridden"]) == {"product_name", "business_context"}
    # workspace-owned repos are untouched by instance-context edits
    slugs = {p["slug"] for p in client.get("/api/projects", headers=AUTH).json()}
    assert "gumo" in slugs

    r = client.delete("/api/context", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["context"]["product_name"] == "Gumo"
    assert r.json()["overridden"] == []


def test_workspace_crud_and_repo_move(client):
    """Phase 2: repos live on workspaces; slugs are globally unique; the
    live-jobs warning rides workspace PATCH responses."""
    ws_list = client.get("/api/workspaces", headers=AUTH).json()
    assert len(ws_list) == 1 and ws_list[0]["slug"] == "default"
    assert "gumo" in ws_list[0]["repos"]  # migration wrapped the §10 map
    default_id = ws_list[0]["id"]

    # all-numeric slugs must not be misrouted into id lookups (finding 1595569):
    # creating '123' twice yields a clean 400, not a 500
    assert client.post("/api/workspaces", headers=AUTH,
                       json={"slug": "123", "name": "Numeric"}).status_code == 200
    r = client.post("/api/workspaces", headers=AUTH, json={"slug": "123", "name": "Numeric"})
    assert r.status_code == 400 and "already exists" in r.json()["detail"]

    # unknown workspace on PATCH is a 404 (matches the members endpoint), not a 400
    assert client.patch("/api/workspaces/9999", headers=AUTH,
                        json={"name": "x"}).status_code == 404
    # a blank name is refused; sync clears the merged map when repos empty
    default_id_probe = ws_list[0]["id"]
    assert client.patch(f"/api/workspaces/{default_id_probe}", headers=AUTH,
                        json={"name": "  "}).status_code == 400
    svc_probe = client.app.state.workspaces
    import json as _json
    svc_probe.store.workspace_repos_replace(default_id_probe, [])
    svc_probe.sync_settings()
    assert _json.loads(svc_probe.settings.repo_map) == {}  # no stale dispatch map
    # restore for the rest of the test
    svc_probe.store.workspace_repos_replace(default_id_probe, [
        {"slug": s, **e} for s, e in ws_list[0]["repos"].items()])
    svc_probe.sync_settings()

    # invalid repos payloads are client errors, not 500s: validate_repo_map's
    # plain ValueError must surface as a 400 (finding 1595977)
    assert client.patch(f"/api/workspaces/{default_id_probe}", headers=AUTH,
                        json={"repos": []}).status_code == 400
    r = client.patch(f"/api/workspaces/{default_id_probe}", headers=AUTH,
                     json={"repos": [{"slug": "bad", "repo": "not-owner-slash-name"}]})
    assert r.status_code == 400 and "owner/name" in r.json()["detail"]

    # a second workspace cannot steal an existing slug
    r = client.post("/api/workspaces", headers=AUTH,
                    json={"slug": "app", "name": "App"})
    assert r.status_code == 200
    app_id = r.json()["id"]
    r = client.patch(f"/api/workspaces/{app_id}", headers=AUTH,
                     json={"repos": [{"slug": "gumo", "repo": "acme/api"}]})
    assert r.status_code == 400 and "already used" in r.json()["detail"]

    # enabling ClickUp without the workspace's own list id would silently
    # route tickets into the instance-global list — refused (finding 1595595)
    r = client.patch(f"/api/workspaces/{app_id}", headers=AUTH,
                     json={"clickup_enabled": True})
    assert r.status_code == 400 and "list id" in r.json()["detail"]
    r = client.patch(f"/api/workspaces/{app_id}", headers=AUTH,
                     json={"clickup_enabled": True, "clickup_list_id": "9016000000"})
    assert r.status_code == 200 and r.json()["clickup_enabled"] is True

    # valid repos + canonical
    r = client.patch(f"/api/workspaces/{app_id}", headers=AUTH,
                     json={"repos": [{"slug": "acme-api", "repo": "acme/api"}],
                           "canonical_project": "acme-api",
                           "workspace_context": "The Acme mobile app."})
    assert r.status_code == 200
    assert r.json()["repos"]["acme-api"]["base"] == "main"

    # removing a slug that live jobs reference warns on the PATCH
    store = client.app.state.store
    store.insert("task-w1", source="manual", title="t", project="acme-api", kind="task")
    store.set_fields("task-w1", workspace_id=app_id)
    r = client.patch(f"/api/workspaces/{app_id}", headers=AUTH,
                     json={"repos": [{"slug": "acme-api2", "repo": "acme/api"}],
                           "canonical_project": "acme-api2"})
    assert r.status_code == 200 and "task-w1" in r.json()["warning"]

    # a workspace without its own canonical gets NO product scope — never a
    # cross-workspace borrow of the instance canonical (finding 1595794)
    svc = client.app.state.workspaces
    client.post("/api/workspaces", headers=AUTH, json={"slug": "bare", "name": "Bare"})
    bare_id = [w["id"] for w in client.get("/api/workspaces", headers=AUTH).json()
               if w["slug"] == "bare"][0]
    client.patch(f"/api/workspaces/{bare_id}", headers=AUTH,
                 json={"repos": [{"slug": "bare-api", "repo": "bare/api"}]})
    assert svc.canonical_for("bare-api") == ""      # own workspace, no canonical
    assert svc.canonical_for("gumo") == "gumo"      # own workspace's canonical
    assert svc.canonical_for("unmapped") == "gumo"  # legacy fallback only when unmapped

    # membership enforcement: a member sees only assigned workspaces' jobs
    client.post("/api/users", headers=AUTH,
                json={"username": "wsdev", "password": "devpass123"})
    member = {"Authorization": "Basic " + base64.b64encode(b"wsdev:devpass123").decode()}
    assert client.get("/api/workspaces", headers=member).json() == []
    assert client.get("/api/jobs/task-w1/session", headers=member).status_code == 404
    client.put(f"/api/workspaces/{app_id}/members", headers=AUTH,
               json={"username": "wsdev", "member": True})
    assert [w["slug"] for w in client.get("/api/workspaces", headers=member).json()] == ["app"]
    assert client.get("/api/jobs/task-w1/session", headers=member).status_code == 200
    # ...but not the default workspace's jobs
    store.insert("task-w2", source="manual", title="t", project="gumo", kind="task")
    store.set_fields("task-w2", workspace_id=default_id)
    assert client.get("/api/jobs/task-w2/session", headers=member).status_code == 404
    ids = {j["issue_id"] for j in client.get("/api/jobs", headers=member).json()}
    assert "task-w1" in ids and "task-w2" not in ids


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

    # gate answers are attributed to the acting user (dev1 needs workspace access)
    store = client.app.state.store
    default_ws = client.get("/api/workspaces", headers=AUTH).json()[0]["id"]
    client.put(f"/api/workspaces/{default_ws}/members", headers=AUTH,
               json={"username": "dev1", "member": True})
    store.insert("task-attr", source="manual", title="t", project="gumo", kind="task")
    store.set_fields("task-attr", status="awaiting_input", analysis="a", question="q",
                     workspace_id=default_ws)
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


def test_bad_basic_never_falls_back_to_cookie(client):
    """An explicit Authorization header that fails must fail the request —
    not silently downgrade to whatever session cookie is in the jar — and a
    lockout surfaces as 429, not a generic 401 (sentry finding 1595191)."""
    assert client.post("/api/login",
                       json={"username": "gumo", "password": "test"}).status_code == 200
    assert client.get("/api/me").status_code == 200  # cookie works
    bad = {"Authorization": "Basic " + base64.b64encode(b"gumo:wrong").decode()}
    assert client.get("/api/me", headers=bad).status_code == 401  # no cookie fallback
    assert client.get("/api/me").status_code == 200  # cookie untouched without the header
    # drive the account into lockout via bad Basic, then the SAME bad header
    # yields 429 (locked) rather than 401 — the status is not masked
    for _ in range(4):
        client.get("/api/me", headers=bad)
    assert client.get("/api/me", headers=bad).status_code == 429


def test_lockout_after_repeated_failures(client):
    client.post("/api/users", headers=AUTH,
                json={"username": "dev2", "password": "devpass123"})
    for _ in range(5):
        assert client.post("/api/login", json={"username": "dev2",
                                               "password": "wrong"}).status_code == 401
    r = client.post("/api/login", json={"username": "dev2", "password": "devpass123"})
    assert r.status_code == 429  # locked even with the right password


def test_setup_wizard_lifecycle(client):
    """§14: a fresh install gets the checklist with untouched steps unticked;
    real configuration ticks them; dismiss persists and survives a context
    reset; members never see instance onboarding."""
    data = client.get("/api/setup", headers=AUTH).json()
    assert data["needed"] is True
    # code-default Gumo context/repos are not "configured" — they must be made
    # the operator's own (semantic compare vs the normalized default map)
    assert data["steps"]["business_context"] is False
    assert data["steps"]["repos"] is False
    assert data["steps"]["team"] is False
    client.put("/api/context", headers=AUTH, json={"product_name": "Acme"})
    assert client.get("/api/setup", headers=AUTH).json()["steps"]["business_context"] is True
    client.post("/api/users", headers=AUTH, json={"username": "m1", "password": "password1"})
    assert client.get("/api/setup", headers=AUTH).json()["steps"]["team"] is True
    mem = {"Authorization": "Basic " + base64.b64encode(b"m1:password1").decode()}
    assert client.get("/api/setup", headers=mem).status_code == 403
    assert client.post("/api/setup/dismiss", headers=AUTH).status_code == 200
    assert client.get("/api/setup", headers=AUTH).json()["needed"] is False
    # a context reset clears overrides but must NOT resurrect the wizard
    client.delete("/api/context", headers=AUTH)
    assert client.get("/api/setup", headers=AUTH).json()["needed"] is False


def test_run_transcripts_recorded_and_scoped(client):
    """§13: run transcripts are written write-through, indexed and replayable
    via the API, and 404-scoped exactly like the job they belong to."""
    from app import transcripts

    store = client.app.state.store
    settings = client.app.state.settings
    ws = client.get("/api/workspaces", headers=AUTH).json()[0]
    store.insert("task-tr1", source="manual", title="t", project="gumo", kind="task")
    store.set_fields("task-tr1", workspace_id=ws["id"])
    # simulate a run writing its activity through the writer
    w = transcripts.open_writer(settings, "task-tr1", "v1-p1-123", {"kind": "v1", "phase": 1})
    w.write("status", "Read app/x.py")
    w.write("delta", "part one ")
    w.write("delta", "part two")
    w.close("pr_opened")

    idx = client.get("/api/jobs/task-tr1/transcripts", headers=AUTH).json()["transcripts"]
    assert [t["key"] for t in idx] == ["v1-p1-123"]
    assert idx[0]["header"] == {"kind": "v1", "phase": 1}
    snap = client.get("/api/jobs/task-tr1/session", headers=AUTH).json()
    assert [t["key"] for t in snap["transcripts"]] == ["v1-p1-123"]
    ev = client.get("/api/jobs/task-tr1/transcripts/v1-p1-123", headers=AUTH).json()["events"]
    assert [e["e"] for e in ev] == ["start", "status", "delta", "delta", "end"]
    assert ev[-1]["d"] == "pr_opened"
    # unknown and traversal-shaped keys are clean 404s, never file errors
    assert client.get("/api/jobs/task-tr1/transcripts/none", headers=AUTH).status_code == 404
    assert client.get("/api/jobs/task-tr1/transcripts/..%2Fbrain.db",
                      headers=AUTH).status_code == 404
    # membership scoping matches the job: no workspace -> 404, assigned -> 200
    client.post("/api/users", headers=AUTH, json={"username": "tmem", "password": "password1"})
    mem = {"Authorization": "Basic " + base64.b64encode(b"tmem:password1").decode()}
    assert client.get("/api/jobs/task-tr1/transcripts", headers=mem).status_code == 404
    assert client.get("/api/jobs/task-tr1/transcripts/v1-p1-123",
                      headers=mem).status_code == 404
    client.put(f"/api/workspaces/{ws['id']}/members", headers=AUTH,
               json={"username": "tmem", "member": True})
    assert client.get("/api/jobs/task-tr1/transcripts", headers=mem).status_code == 200


def test_transcript_writer_cap_and_prune(client):
    """The writer caps at 2MB with an explicit marker and never buffers; the
    janitor prunes by mtime."""
    from app import transcripts

    settings = client.app.state.settings
    w = transcripts.open_writer(settings, "capjob", "big", {})
    w._bytes = transcripts.TRANSCRIPT_CAP_BYTES  # simulate a full file
    w.write("delta", "overflow")
    w.write("delta", "silently dropped")
    w.close("ok")
    ev = transcripts.read_events(settings, "capjob", "big")
    assert [e["e"] for e in ev] == ["start", "truncated"]
    assert transcripts.prune(settings, ttl_days=-1) == 1  # everything is "old"
    assert transcripts.read_events(settings, "capjob", "big") is None


def test_change_password_rotates_session(client):
    """A self password change revokes every OTHER session and the old token,
    but the caller stays signed in on a freshly rotated cookie — being dumped
    to the login page made a successful temp-password change look broken."""
    client.post("/api/users", headers=AUTH,
                json={"username": "dev3", "password": "devpass123"})
    login = client.post("/api/login", json={"username": "dev3", "password": "devpass123"})
    assert login.status_code == 200
    old_token = client.cookies.get("ctrlloop_session")
    r = client.post("/api/me/password", json={"current": "devpass123", "new": "newpass456"})
    assert r.status_code == 200
    new_token = client.cookies.get("ctrlloop_session")
    assert new_token and new_token != old_token  # rotated, not reused
    me = client.get("/api/me")  # the fresh cookie keeps this browser signed in
    assert me.status_code == 200 and me.json()["must_change_pw"] is False
    # the pre-change token (any other device/session) is dead
    client.cookies.set("ctrlloop_session", old_token)
    assert client.get("/api/me").status_code == 401
    fresh = {"Authorization": "Basic " + base64.b64encode(b"dev3:newpass456").decode()}
    assert client.get("/api/me", headers=fresh).json()["must_change_pw"] is False

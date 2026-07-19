"""Epic E2 — scoped API tokens + HTTP Basic deprecation."""

import base64
import time

import pytest
from fastapi.testclient import TestClient


AUTH = {"Authorization": "Basic " + base64.b64encode(b"gumo:test").decode()}


def _basic(username, password):
    raw = f"{username}:{password}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


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


def test_create_returns_plaintext_once(client):
    r = client.post("/api/tokens", headers=AUTH, json={"name": "ci"})
    assert r.status_code == 200
    body = r.json()
    assert body["token"].startswith("ctl_")
    assert body["prefix_hint"].startswith("ctl_…")
    # the leading secret chars are NOT in the hint
    assert body["token"][4:12] not in body["prefix_hint"]
    # list never returns the secret
    lst = client.get("/api/tokens", headers=AUTH).json()
    assert lst[0]["id"] == body["id"]
    assert "token" not in lst[0]


def test_bearer_auth_resolves_user(client):
    token = client.post("/api/tokens", headers=AUTH, json={}).json()["token"]
    r = client.get("/api/me", headers=_bearer(token))
    assert r.status_code == 200
    assert r.json()["username"] == "gumo"


def test_bad_bearer_hard_401(client):
    assert client.get("/api/me", headers=_bearer("ctl_bogus")).status_code == 401
    # a non-ctl bearer is reserved -> 401, never falls through to a cookie
    assert client.get("/api/me", headers=_bearer("something")).status_code == 401


def test_revoked_and_expired_rejected(client):
    store = client._store
    # revoked
    body = client.post("/api/tokens", headers=AUTH, json={}).json()
    assert client.get("/api/me", headers=_bearer(body["token"])).status_code == 200
    assert client.delete(f"/api/tokens/{body['id']}", headers=AUTH).status_code == 200
    assert client.get("/api/me", headers=_bearer(body["token"])).status_code == 401
    # expired (created directly with a past expiry)
    gumo = store.user_get("gumo")
    tok, row = store.api_token_create(gumo["id"], name="exp", ttl_days=0)
    with store._conn() as c:
        c.execute("UPDATE api_tokens SET expires_at = ? WHERE id = ?",
                  (time.time() - 10, row["id"]))
    assert client.get("/api/me", headers=_bearer(tok)).status_code == 401


def test_last_used_touched(client):
    store = client._store
    token = client.post("/api/tokens", headers=AUTH, json={}).json()["token"]
    client.get("/api/me", headers=_bearer(token))
    gumo = store.user_get("gumo")
    rows = store.api_token_list(gumo["id"])
    assert rows[0]["last_used_at"] is not None


def test_basic_deprecated_for_member_but_admin_break_glass(client):
    store = client._store
    # admin creates a member with a known password
    r = client.post("/api/users", headers=AUTH,
                    json={"username": "bot", "password": "botpassw0rd", "role": "member"})
    assert r.status_code in (200, 201)
    member_auth = _basic("bot", "botpassw0rd")
    # member Basic works before tokening
    assert client.get("/api/me", headers=member_auth).status_code == 200
    # member mints a token via Basic
    tok = client.post("/api/tokens", headers=member_auth, json={}).json()["token"]
    # now Basic is deprecated for this member -> 403; the token still works
    assert client.get("/api/me", headers=member_auth).status_code == 403
    assert client.get("/api/me", headers=_bearer(tok)).status_code == 200
    # the admin (break-glass) can still use Basic even after minting a token
    client.post("/api/tokens", headers=AUTH, json={})
    assert client.get("/api/me", headers=AUTH).status_code == 200


def test_admin_revoke_other_users_token(client):
    store = client._store
    client.post("/api/users", headers=AUTH,
                json={"username": "bot2", "password": "botpassw0rd", "role": "member"})
    member_auth = _basic("bot2", "botpassw0rd")
    tok = client.post("/api/tokens", headers=member_auth, json={}).json()["token"]
    # admin lists + revokes the member's token
    lst = client.get("/api/users/bot2/tokens", headers=AUTH).json()
    tid = lst[0]["id"]
    assert client.delete(f"/api/users/bot2/tokens/{tid}", headers=AUTH).status_code == 200
    assert client.get("/api/me", headers=_bearer(tok)).status_code == 401


def test_token_lifecycle_audited(client):
    store = client._store
    body = client.post("/api/tokens", headers=AUTH, json={"name": "x"}).json()
    client.delete(f"/api/tokens/{body['id']}", headers=AUTH)
    actions = [r["action"] for r in store.audit_recent()]
    assert "token.create" in actions
    assert "token.revoke" in actions

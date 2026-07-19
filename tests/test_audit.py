"""Epic E4 — append-only audit_log unification, coverage, export."""

import base64
import json

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
        c._store = main_module.app.state.store
        yield c
    config.get_settings.cache_clear()


def test_admin_event_shim_writes_audit_log(store):
    store.admin_event_add("workspace_config", target="7", detail="stage_role_map changed",
                          actor="dashboard:alice")
    rows = store.audit_recent()
    assert any(r["action"] == "config.workspace" and r["actor"] == "dashboard:alice"
               for r in rows)
    # legacy admin_events row still written (rollback safety)
    assert store.admin_events_recent()


def test_boot_copy_idempotent_via_marker(tmp_path):
    from app.db import JobStore
    path = str(tmp_path / "b.db")
    s1 = JobStore(path)
    # write a legacy admin_events row directly, then re-open: boot-copy must NOT
    # re-run (marker present), so the row is not double-copied.
    with s1._conn() as c:
        c.execute("INSERT INTO admin_events (kind, target, detail, actor, at) "
                  "VALUES ('clickup_link', 'bob', '', 'dashboard:a', 123.0)")
    before = len(s1.audit_page(0, 5000))
    JobStore(path)  # re-open — marker already set on first init
    s2 = JobStore(path)
    after = len(s2.audit_page(0, 5000))
    assert after == before  # no re-copy


def test_boot_copy_migrates_preexisting_legacy_rows(tmp_path):
    import sqlite3
    from app.db import JobStore, SCHEMA
    path = str(tmp_path / "legacy.db")
    # simulate an OLD db that has admin_events rows but no marker / no audit_log
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO admin_events (kind, target, detail, actor, at) "
                 "VALUES ('clickup_link', 'bob', 'linked', 'dashboard:a', 100.0)")
    conn.execute("DROP TABLE audit_log")
    conn.execute("DELETE FROM app_config WHERE key='audit_legacy_copied'")
    conn.commit()
    conn.close()
    s = JobStore(path)  # boot-copy runs
    rows = s.audit_page(0, 5000)
    assert any(r["action"] == "user.clickup_link" for r in rows)


def test_gate_decision_audited_both_channels(store, settings):
    import asyncio
    from app.worker import Worker
    w = Worker(settings, store)
    # a v1 task job parked at a gate, no DRIs -> inert enforcement (anyone answers)
    store.insert("t1", source="dashboard", kind="task", project="web", title="x")
    store.set_fields("t1", status="awaiting_input", question="approve?")
    asyncio.run(w.answer_job("t1", "skip", "", via="dashboard:alice"))
    dash = [r for r in store.audit_recent() if r["action"] == "gate.decision"]
    assert dash and dash[0]["channel"] == "dashboard"

    store.insert("t2", source="dashboard", kind="task", project="web", title="y")
    store.set_fields("t2", status="awaiting_input", question="approve?")
    asyncio.run(w.answer_job("t2", "skip", "", via="clickup:bob#42"))
    cu = [r for r in store.audit_recent() if r["action"] == "gate.decision"
          and r["job_id"] == "t2"]
    assert cu and cu[0]["channel"] == "clickup"


def test_login_audited(client):
    client.post("/api/login", json={"username": "gumo", "password": "test"})
    rows = client._store.audit_recent()
    assert any(r["action"] == "auth.login" and r["detail"] and "password" in r["detail"]
               for r in rows)


def test_export_jsonl_cursor_paged_and_monotonic(client):
    store = client._store
    # generate several rows
    for i in range(7):
        store.audit_add("test.action", actor="engine", target=str(i))
    r = client.get("/api/audit/export?after=0&limit=3", headers=AUTH)
    assert r.status_code == 200
    lines = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    assert len(lines) == 3
    ids = [o["id"] for o in lines]
    assert ids == sorted(ids)  # monotonic
    cursor = int(r.headers["X-Next-Cursor"])
    assert cursor == ids[-1]
    # next page continues after the cursor with no overlap
    r2 = client.get(f"/api/audit/export?after={cursor}&limit=3", headers=AUTH)
    lines2 = [json.loads(l) for l in r2.text.splitlines() if l.strip()]
    assert all(o["id"] > cursor for o in lines2)


def test_export_requires_admin(client):
    # create a member and confirm they cannot export
    client.post("/api/users", headers=AUTH,
                json={"username": "m1", "password": "memberpass1", "role": "member"})
    member = {"Authorization": "Basic " + base64.b64encode(b"m1:memberpass1").decode()}
    assert client.get("/api/audit/export", headers=member).status_code == 403


def test_detail_redaction_allow_list(store):
    store.audit_add("test.secret", actor="engine",
                    detail={"name": "ok", "client_secret": "SHOULD_NOT_APPEAR",
                            "password": "SHOULD_NOT_APPEAR"})
    row = store.audit_recent()[0]
    assert "SHOULD_NOT_APPEAR" not in row["detail"]
    assert "ok" in row["detail"]

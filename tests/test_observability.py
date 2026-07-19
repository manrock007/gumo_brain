"""Epic F4: observability — /metrics exposition + auth gate, /health/ready,
JSON log shape, request_id/job_id contextvar propagation."""

import base64
import json
import logging

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


def test_health_ready_200_when_db_up(client):
    r = client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["checks"]["db"] is True
    assert set(body["checks"]) == {"db", "worker", "scheduler"}


def test_health_ready_503_when_db_down(client, monkeypatch):
    # simulate the hard dependency being down
    from app import main as main_module
    monkeypatch.setattr(main_module.app.state.store, "ping", lambda: False)
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["ready"] is False


def test_health_ready_never_leaks_error_bodies(client, monkeypatch):
    from app import main as main_module
    monkeypatch.setattr(main_module.app.state.store, "ping", lambda: False)
    body = client.get("/health/ready").json()
    # only booleans in checks — no strings/credentials/upstream error text
    assert all(isinstance(v, bool) for v in body["checks"].values())


def test_metrics_requires_admin_by_default(client):
    assert client.get("/metrics").status_code == 401  # no auth
    r = client.get("/metrics", headers=AUTH)
    assert r.status_code == 200


def test_metrics_exposition_shape(client):
    r = client.get("/metrics", headers=AUTH)
    assert r.status_code == 200
    text = r.text
    assert "ctrlloop_build_info" in text
    assert "ctrlloop_queue_depth" in text
    assert "ctrlloop_jobs_total" in text
    assert "ctrlloop_runs_today" in text
    assert "ctrlloop_stage_run_cost_usd_total" in text
    assert "ctrlloop_gate_latency_seconds_count" in text
    # valid exposition: HELP/TYPE lines present
    assert "# TYPE ctrlloop_queue_depth gauge" in text


def test_metrics_token_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test")
    monkeypatch.setenv("METRICS_TOKEN", "scrape-secret")
    from app import config
    config.get_settings.cache_clear()
    import importlib
    from app import main as main_module
    importlib.reload(main_module)
    with TestClient(main_module.app) as c:
        # admin session no longer suffices — bearer token required
        assert c.get("/metrics", headers=AUTH).status_code == 401
        ok = c.get("/metrics", headers={"Authorization": "Bearer scrape-secret"})
        assert ok.status_code == 200
    config.get_settings.cache_clear()


def test_request_id_echoed(client):
    r = client.get("/health", headers={"X-Request-ID": "req-abc"})
    assert r.headers.get("X-Request-ID") == "req-abc"
    # minted when absent
    r2 = client.get("/health")
    assert r2.headers.get("X-Request-ID")


# ---- logconfig ----

def test_json_formatter_shape():
    from app import logconfig
    rec = logging.LogRecord("brain.x", logging.INFO, "f", 1, "hello %s", ("world",), None)
    rec.request_id = "req-1"
    rec.job_id = "job-9"
    line = logconfig.JsonFormatter().format(rec)
    obj = json.loads(line)
    assert obj["level"] == "INFO"
    assert obj["logger"] == "brain.x"
    assert obj["msg"] == "hello world"
    assert obj["request_id"] == "req-1"
    assert obj["job_id"] == "job-9"
    assert "ts" in obj


def test_logctx_binds_and_resets():
    from app import logconfig
    assert logconfig.job_id_var.get() is None
    with logconfig.logctx(job_id="j1", request_id="r1"):
        assert logconfig.job_id_var.get() == "j1"
        assert logconfig.request_id_var.get() == "r1"
    assert logconfig.job_id_var.get() is None  # reset on exit
    assert logconfig.request_id_var.get() is None


def test_configure_logging_text_default_is_idempotent():
    from app import logconfig
    from app.config import Settings
    root = logging.getLogger()
    logconfig.configure_logging(Settings(log_format="text"))
    owned = [h for h in root.handlers if getattr(h, "_ctrlloop_owned", False)]
    logconfig.configure_logging(Settings(log_format="text"))
    owned2 = [h for h in root.handlers if getattr(h, "_ctrlloop_owned", False)]
    assert len(owned2) == 1  # re-call replaces, never stacks

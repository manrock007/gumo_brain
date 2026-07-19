import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Settings are read at import time in app.main — pin a deterministic test env
# before anything imports the app. The code defaults are NEUTRAL (empty repo
# map, no canonical), so the suite pins a small example map; tests that need
# the true from-zero state (tests/test_install_from_zero.py) delenv these.
os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("CLICKUP_TOKEN", "")  # ClickUp disabled: pure best-effort no-ops
os.environ.setdefault("SWEEP_ENABLED", "false")
os.environ.setdefault("REPO_MAP", json.dumps({
    "demo": {"repo": "acme/demo", "base": "main"},
    "web": {"repo": "acme/web", "base": "main"},
    "react-native": {"repo": "acme/mobile", "base": "main"},
}))
os.environ.setdefault("MEMORY_CANONICAL_PROJECT", "demo")


@pytest.fixture()
def store(tmp_path):
    from app.db import JobStore

    return JobStore(str(tmp_path / "brain.db"))


@pytest.fixture()
def settings(tmp_path):
    from app.config import Settings

    return Settings(data_dir=str(tmp_path), dashboard_password="test")


@pytest.fixture()
def worker(settings, store):
    from app.worker import Worker

    return Worker(settings, store)

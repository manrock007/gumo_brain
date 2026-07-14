import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Settings are read at import time in app.main — pin a deterministic test env
# before anything imports the app.
os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("CLICKUP_TOKEN", "")  # ClickUp disabled: pure best-effort no-ops
os.environ.setdefault("SWEEP_ENABLED", "false")


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

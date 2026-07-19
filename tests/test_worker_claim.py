"""Epic F2: multi-worker DB-claim queue — the SQLite-safe surface (the PG
SKIP-LOCKED no-double-claim and advisory-serialization tests are PG-gated in
test_dbdriver_cas.py). Asserts the single-process SQLite path is unchanged."""

import asyncio

import pytest

from app.config import Settings
from app.db import JobStore
from app.repolocks import (InProcessRepoLocks, PgAdvisoryRepoLocks, advisory_key,
                           resolve_locks)


def _store(tmp_path):
    return JobStore(str(tmp_path / "t.db"))


def test_claim_next_job_is_none_on_sqlite(tmp_path):
    store = _store(tmp_path)
    store.insert("j1", source="manual", kind="task")
    assert store.claim_next_job("worker-a") is None  # SQLite never claims


def _running(store, issue_id, claimed_by):
    store.insert(issue_id, source="manual", kind="task")
    store.set_status(issue_id, "running")
    store.set_fields(issue_id, claimed_by=claimed_by, run_started_at=1.0)


def test_recover_worker_claims_scoped(tmp_path):
    store = _store(tmp_path)
    # two 'running' rows claimed by different workers
    _running(store, "r-mine", "w1")
    _running(store, "r-other", "w2")
    recovered = store.recover_worker_claims("w1")
    ids = {r["issue_id"] for r in recovered}
    assert ids == {"r-mine"}  # only this worker's claim reset
    assert store.get("r-mine")["status"] == "queued"
    assert store.get("r-mine")["claimed_by"] == ""
    assert store.get("r-other")["status"] == "running"  # sibling untouched


def test_release_claim_clears_ownership(tmp_path):
    store = _store(tmp_path)
    _running(store, "rc", "w1")
    store.release_claim("rc")
    assert store.get("rc")["claimed_by"] == ""


def test_resolve_locks_sqlite_is_in_process(tmp_path):
    store = _store(tmp_path)
    locks = resolve_locks(store)
    assert isinstance(locks, InProcessRepoLocks)


def test_settings_multi_worker_flag():
    assert Settings(database_url="").multi_worker is False
    assert Settings(database_url="postgresql://x/y").multi_worker is True
    assert Settings(database_url="postgresql://x/y").db_backend == "postgres"


def test_resolved_worker_id_defaults_to_host_pid():
    s = Settings(worker_id="")
    wid = s.resolved_worker_id()
    assert ":" in wid  # hostname:pid
    assert Settings(worker_id="w-explicit").resolved_worker_id() == "w-explicit"


def test_advisory_key_is_stable_and_distinct():
    assert advisory_key("repo:acme/api") == advisory_key("repo:acme/api")
    assert advisory_key("repo:acme/api") != advisory_key("repo:acme/web")
    assert -(2 ** 63) <= advisory_key("x") < 2 ** 63  # fits a bigint


def test_in_process_locks_serialize_per_repo():
    locks = InProcessRepoLocks()
    lk = locks.for_repo("acme/api")
    assert lk is locks.for_repo("acme/api")  # same lock per repo
    assert lk is not locks.for_repo("acme/web")


def test_pg_advisory_locks_return_async_contexts():
    # construct without a live DB: just assert the interface shape
    class _FakeDriver:
        backend = "postgres"
    locks = PgAdvisoryRepoLocks(_FakeDriver())
    assert hasattr(locks.for_repo("acme/api"), "__aenter__")
    assert hasattr(locks.claude_global, "__aenter__")
    assert hasattr(locks.chat_global, "__aenter__")

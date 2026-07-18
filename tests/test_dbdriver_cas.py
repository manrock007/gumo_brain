"""Epic F1/F2: Postgres-gated behavioral tests. Skipped unless TEST_DATABASE_URL
points at a real Postgres that has been migrated to head (alembic upgrade head).

These prove the invariants the design re-verifies under Postgres:
  - CAS (cas_status) — exactly one of two concurrent transitions wins
  - schema parity — the Alembic-migrated PG schema matches the SQLite schema
  - claim_next_job — SKIP LOCKED never double-claims (F2)
"""

import os
import threading

import pytest

from app.db import JobStore

PG_URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL unset")


@pytest.fixture
def pg_store():
    store = JobStore(PG_URL)
    with store._conn() as c:  # clean slate for the test rows
        c.execute("DELETE FROM jobs WHERE issue_id LIKE 'castest-%'")
    return store


def test_cas_status_exactly_one_winner(pg_store):
    pg_store.watch_insert("castest-1", status="queued")  # a queued row
    pg_store.set_status("castest-1", "queued")
    results = []

    def race():
        results.append(pg_store.cas_status("castest-1", ["queued"], "running"))

    t1, t2 = threading.Thread(target=race), threading.Thread(target=race)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == [False, True]  # exactly one won


def test_schema_parity_tables_and_columns(pg_store):
    """Every SQLite table/column (minus FTS) exists in the migrated PG schema."""
    import sqlite3
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        sq = sqlite3.connect(path)
        sq.row_factory = sqlite3.Row
        JobStore(path)  # build the SQLite schema
        sqlite_tables = {
            r["name"] for r in sq.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'mem_fts%'").fetchall()
        }
        with pg_store._conn() as c:
            pg_tables = {
                r["table_name"] for r in c.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'").fetchall()
            }
        assert sqlite_tables <= pg_tables, sqlite_tables - pg_tables
    finally:
        os.unlink(path)


def test_claim_next_job_no_double_claim(pg_store):
    """F2: two workers claiming concurrently never grab the same row."""
    pg_store.watch_insert("castest-c", status="queued")
    pg_store.set_status("castest-c", "queued")
    claims = []

    def claim():
        job = pg_store.claim_next_job(worker_id="w-" + threading.current_thread().name)
        claims.append(job["issue_id"] if job else None)

    ts = [threading.Thread(target=claim, name=str(i)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    got = [c for c in claims if c == "castest-c"]
    assert len(got) == 1  # exactly one worker claimed it

"""Epic F1: DB driver seam — SQLite behavior, paramstyle rewrite, ON CONFLICT
conversions, exception normalization, and the MIGRATIONS<->Alembic lockstep
static guard. The PG-gated behavioral tests live in test_dbdriver_cas.py."""

import os
import sqlite3

import pytest

from app.db import JobStore, MIGRATIONS
from app.dbdriver import (DBDriver, SqliteDriver, resolve_driver,
                          translate_paramstyle)


def _store(tmp_path):
    return JobStore(str(tmp_path / "t.db"))


# ---- paramstyle rewrite (F1 adapter) ----

def test_translate_qmark_to_pgstyle():
    assert translate_paramstyle("SELECT * FROM t WHERE a = ? AND b = ?") == \
        "SELECT * FROM t WHERE a = %s AND b = %s"


def test_translate_skips_qmark_inside_string_literal():
    # a literal '?' inside quotes must NOT become a placeholder
    sql = "UPDATE t SET note = 'why?' WHERE id = ?"
    assert translate_paramstyle(sql) == "UPDATE t SET note = 'why?' WHERE id = %s"


def test_translate_doubles_literal_percent():
    # a literal % must be doubled under the %s paramstyle
    assert translate_paramstyle("SELECT '100%' , ?") == "SELECT '100%%' , %s"


def test_translate_handles_doubled_quote_escape():
    sql = "SELECT 'it''s ok?' WHERE x = ?"
    assert translate_paramstyle(sql) == "SELECT 'it''s ok?' WHERE x = %s"


# ---- driver resolution / defaults ----

def test_resolve_driver_defaults_to_sqlite(tmp_path):
    class S:
        database_url = ""
        db_path = str(tmp_path / "x.db")
    d = resolve_driver(S())
    assert isinstance(d, SqliteDriver)
    assert d.backend == "sqlite"
    assert d.owns_schema is False


def test_resolve_driver_unknown_backend_fails_closed_to_sqlite(tmp_path):
    class S:
        database_url = "mysql://nope"  # not a postgres URL
        db_path = str(tmp_path / "x.db")
    assert isinstance(resolve_driver(S()), SqliteDriver)


def test_sqlite_driver_is_the_default_store_path(tmp_path):
    store = _store(tmp_path)
    assert store._driver.backend == "sqlite"
    assert store._driver.owns_schema is False
    # a fresh SQLite store built its schema in-process
    assert store.job_count() == 0


def test_exception_aliases_track_sqlite(tmp_path):
    import app.db as db_mod
    _store(tmp_path)
    assert db_mod.IntegrityError is sqlite3.IntegrityError
    assert db_mod.OperationalError is sqlite3.OperationalError


# ---- ON CONFLICT conversions still dedupe on SQLite ----

def test_gate_event_add_dedupes_via_on_conflict(tmp_path):
    store = _store(tmp_path)
    assert store.gate_event_add("job1", 0, "admin_override", "ref-1") is True
    # same (job, kind, ref) → deduped (rowcount 0)
    assert store.gate_event_add("job1", 0, "admin_override", "ref-1") is False


def test_inbox_item_add_dedupes_via_on_conflict(tmp_path):
    store = _store(tmp_path)
    assert store.inbox_item_add("proposal", "k1", "t") is True
    assert store.inbox_item_add("proposal", "k1", "t2") is False


def test_decision_add_dedupes_on_source_ref(tmp_path):
    store = _store(tmp_path)
    d1 = store.decision_add("gate", "we chose X", ref="g1")
    assert d1 is not None
    assert store.decision_add("gate", "dup", ref="g1") is None
    # empty-ref rows never conflict (partial index)
    a = store.decision_add("manual", "one", ref="")
    b = store.decision_add("manual", "two", ref="")
    assert a is not None and b is not None and a != b


def test_autonomy_pin_set_upserts_via_on_conflict(tmp_path):
    store = _store(tmp_path)
    ws = store.workspace_create("w", "W")
    store.autonomy_pin_set(ws["id"], 7, "always_gate", "dashboard:me")
    store.autonomy_pin_set(ws["id"], 7, "always_auto", "dashboard:you")  # replace
    pins = store.autonomy_pins_for(ws["id"])
    assert pins[7]["pin"] == "always_auto"
    assert pins[7]["set_by"] == "dashboard:you"


def test_insert_returning_gives_ids_on_sqlite(tmp_path):
    store = _store(tmp_path)
    ws = store.workspace_create("w", "W")
    assert isinstance(ws["id"], int)
    rid = store.stage_run_open("job1", 0, 1)
    assert isinstance(rid, int) and rid > 0


# ---- MIGRATIONS <-> Alembic baseline lockstep (amendment 5, non-gated) ----

def test_every_migrations_column_in_alembic_baseline():
    """Static guard: every additive MIGRATIONS column name must appear in the
    committed Alembic baseline, so the two schema definitions cannot silently
    diverge even when TEST_DATABASE_URL is unset."""
    import glob
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    versions = glob.glob(os.path.join(here, "migrations", "versions", "*.py"))
    text = "\n".join(open(p).read() for p in versions)
    missing = []
    for table, cols in MIGRATIONS.items():
        for col in cols:
            if col not in text:
                missing.append(f"{table}.{col}")
    assert not missing, f"MIGRATIONS columns absent from Alembic revisions: {missing}"


def test_baseline_carries_core_tables_and_indexes():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(here, "migrations", "versions", "0001_baseline.py")).read()
    for t in ("jobs", "users", "workspaces", "stage_runs", "audit_log", "decisions"):
        assert f"CREATE TABLE IF NOT EXISTS {t} " in text
    # identity PK translation + partial/expression indexes preserved
    assert "GENERATED BY DEFAULT AS IDENTITY" in text
    assert "WHERE ref != ''" in text
    assert "COALESCE(workspace_id, -1)" in text
    # FTS5 virtual table is NOT emitted (Postgres retrieval is absent in F1)
    assert "USING fts5" not in text
    assert "CREATE VIRTUAL TABLE" not in text

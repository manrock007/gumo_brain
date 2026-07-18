#!/usr/bin/env python3
"""One-shot SQLite -> Postgres data copy (Epic F1).

Adopting Postgres from an existing SQLite instance:
  1. create an empty Postgres database
  2. ``alembic upgrade head``  (materializes the exact baseline schema)
  3. ``python scripts/sqlite_to_pg.py --sqlite /data/brain.db --pg $DATABASE_URL``

No data loss: the baseline schema is DERIVED from the identical SQLite schema,
so every table/column lines up. This copies table-by-table respecting FK order,
preserves identity primary keys (OVERRIDING SYSTEM VALUE), and resets each
identity sequence with setval so future inserts don't collide.

The FTS5 mem_fts virtual table is intentionally skipped — Postgres retrieval is
absent (out of scope for F1); the index rebuilds itself as memory refreshes.
"""

import argparse
import sqlite3
import sys

# copy in dependency order (parents before children where FKs matter); tables
# not listed are copied afterwards in sqlite_master order.
PREFERRED_ORDER = [
    "app_config", "users", "people", "api_tokens", "auth_sessions",
    "workspaces", "workspace_repos", "workspace_members",
    "jobs", "stage_state", "stage_runs", "artifact_state", "guidance_log",
    "prs", "gate_events", "gate_chat", "decisions", "inbox_items", "frictions",
    "routines", "routine_runs", "autonomy_pins", "autonomy_scores",
    "autonomy_events", "outcomes", "watch_readings", "slack_cursors",
    "audit_log", "admin_events", "metric_readings",
]

SKIP_TABLES = {"mem_fts", "mem_fts_data", "mem_fts_idx", "mem_fts_content",
               "mem_fts_docsize", "mem_fts_config"}


def _identity_columns(pg_cur, table: str) -> list[str]:
    pg_cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s AND is_identity = 'YES'", (table,))
    return [r[0] for r in pg_cur.fetchall()]


def copy(sqlite_path: str, pg_dsn: str):
    import psycopg

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    all_tables = [r["name"] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()]
    tables = [t for t in PREFERRED_ORDER if t in all_tables]
    tables += [t for t in all_tables if t not in tables and t not in SKIP_TABLES]

    with psycopg.connect(pg_dsn) as dst:
        for table in tables:
            if table in SKIP_TABLES:
                continue
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                continue
            cols = rows[0].keys()
            collist = ", ".join(cols)
            placeholders = ", ".join(["%s"] * len(cols))
            with dst.cursor() as cur:
                idcols = _identity_columns(cur, table)
                override = "OVERRIDING SYSTEM VALUE " if idcols else ""
                cur.executemany(
                    f"INSERT INTO {table} ({collist}) {override}VALUES ({placeholders})",
                    [tuple(r[c] for c in cols) for r in rows])
                # reset identity sequences so future inserts continue past the max
                for idcol in idcols:
                    cur.execute(
                        f"SELECT setval(pg_get_serial_sequence(%s, %s), "
                        f"COALESCE((SELECT MAX({idcol}) FROM {table}), 1))",
                        (table, idcol))
            dst.commit()
            print(f"copied {len(rows):>7} rows -> {table}", file=sys.stderr)
    src.close()
    print("done", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", required=True, help="path to the SQLite brain.db")
    ap.add_argument("--pg", required=True, help="postgresql://… DSN of the empty (migrated) DB")
    args = ap.parse_args()
    copy(args.sqlite, args.pg)

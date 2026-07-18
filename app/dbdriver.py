"""Database driver seam (Epic F1).

SQLite stays the zero-config default; Postgres is additive and opt-in via
``database_url``. The seam mirrors the analytics/secrets provider pattern: a
base class, concrete drivers, a factory keyed on a settings field, fail-closed
to the working local path on anything unparseable.

The whole existing test suite exercises ``SqliteDriver`` — it is byte-for-byte
today's ``sqlite3.connect(path)`` + ``row_factory=Row`` with commit-on-exit /
close-in-finally. The Postgres path is only reachable when ``database_url`` is
a ``postgresql://`` DSN (and psycopg is installed — see
requirements-postgres.txt); it is verified by the PG-gated tests that skip when
``TEST_DATABASE_URL`` is unset.

Each driver exposes normalized ``IntegrityError`` / ``OperationalError`` so
call sites catch driver-agnostic types (``db.IntegrityError`` etc.), and an
``owns_schema`` flag: SQLite (False) → JobStore runs SCHEMA/MIGRATIONS in
``__init__``; Postgres (True) → Alembic owns the schema and the DDL/bootstrap
block is skipped.
"""

import logging
import sqlite3
from contextlib import contextmanager

log = logging.getLogger("brain.dbdriver")


def translate_paramstyle(sql: str) -> str:
    """Rewrite qmark placeholders (``?``) to psycopg's ``%s`` in a single pass,
    skipping ``?`` inside string literals and doubling any literal ``%`` (which
    would otherwise be read as a placeholder under the ``%s`` paramstyle).

    The codebase uses only positional ``?`` and has no literal ``%`` in SQL text
    today (LIKE patterns pass ``%``/``_`` as parameters with ESCAPE), so this is
    a no-op-shaped translation that also stays correct if either ever changes.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    quote = ""
    while i < n:
        ch = sql[i]
        if quote:
            # a literal % must be doubled EVERYWHERE under the %s paramstyle,
            # including inside string literals; ? inside a literal stays literal.
            out.append("%%" if ch == "%" else ch)
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:  # doubled '' / "" escape
                    out.append(sql[i + 1])
                    i += 2
                    continue
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


class DBDriver:
    """The seam. Drivers yield a connection whose ``.execute(sql, params)``
    accepts ``?`` placeholders and whose rows behave like ``sqlite3.Row``
    (mapping access + ``dict(row)``), with commit-on-exit / close-in-finally."""

    backend = "base"
    owns_schema = False
    IntegrityError: type = sqlite3.IntegrityError
    OperationalError: type = sqlite3.OperationalError

    def connect(self):  # pragma: no cover - abstract
        raise NotImplementedError


class SqliteDriver(DBDriver):
    """Today's code path, unchanged: ``sqlite3.connect(path)`` + Row factory,
    commit on clean exit, close in finally. owns_schema=False → JobStore runs
    SCHEMA + MIGRATIONS itself."""

    backend = "sqlite"
    owns_schema = False
    IntegrityError = sqlite3.IntegrityError
    OperationalError = sqlite3.OperationalError

    def __init__(self, path: str):
        self.path = path

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


class _PgConn:
    """Thin adapter over a psycopg connection: rewrites qmark SQL to ``%s`` on
    every ``.execute`` and returns the psycopg cursor (dict rows via the pool's
    row_factory). ``dict(row)`` and ``row["col"]`` both work on dict rows."""

    def __init__(self, conn):
        self._c = conn

    def execute(self, sql, params=()):
        return self._c.execute(translate_paramstyle(sql), params)


class PostgresDriver(DBDriver):
    """psycopg3 connection-pool driver. owns_schema=True → Alembic owns the
    schema and JobStore skips its DDL/bootstrap block. psycopg is lazy-imported
    here so a SQLite install never needs the dependency.

    lastrowid is synthesized by JobStore._insert_returning at the ~7 real
    sites (an appended ``RETURNING <pk>``), NOT by a blanket rewrite — most
    tables' primary key is not named ``id``, so a universal ``RETURNING id``
    would break every plain insert.
    """

    backend = "postgres"
    owns_schema = True

    def __init__(self, dsn: str, *, pool_size: int = 5, max_overflow: int = 5,
                 statement_timeout_ms: int = 0):
        import psycopg  # lazy — only Postgres deployments import it
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self.IntegrityError = psycopg.errors.IntegrityError
        self.OperationalError = psycopg.OperationalError

        kwargs: dict = {}
        if statement_timeout_ms and int(statement_timeout_ms) > 0:
            kwargs["options"] = f"-c statement_timeout={int(statement_timeout_ms)}"

        def _configure(conn):
            conn.row_factory = dict_row
            conn.autocommit = False

        size = max(1, int(pool_size))
        self._pool = ConnectionPool(
            dsn, min_size=1, max_size=size + max(0, int(max_overflow)),
            configure=_configure, kwargs=kwargs, open=True)

    @contextmanager
    def connect(self):
        # psycopg's pool connection context commits on clean exit and rolls
        # back on exception — the same commit-on-exit contract as SQLite.
        with self._pool.connection() as conn:
            yield _PgConn(conn)

    def advisory_conn(self):
        """A dedicated pooled connection for session-level advisory locks
        (Epic F2 repolocks). Returns the pool's connection context manager."""
        return self._pool.connection()


def resolve_driver(settings) -> DBDriver:
    """Factory: a ``postgresql://`` database_url selects PostgresDriver, else
    SqliteDriver at settings.db_path. Fail closed — an unknown/unparseable
    backend degrades to the working local SQLite path."""
    url = (getattr(settings, "database_url", "") or "").strip()
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresDriver(
            url,
            pool_size=getattr(settings, "db_pool_size", 5),
            max_overflow=getattr(settings, "db_pool_max_overflow", 5),
            statement_timeout_ms=getattr(settings, "db_statement_timeout_ms", 0),
        )
    return SqliteDriver(settings.db_path)
